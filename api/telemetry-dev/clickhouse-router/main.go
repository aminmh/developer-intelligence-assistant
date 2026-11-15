package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

const targetPath = "/var/lib/devassistant/runtime/router-target.json"

var hostPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$`)

type target struct {
	Mode       string `json:"mode"`
	Host       string `json:"host"`
	HTTPPort   int    `json:"http_port"`
	NativePort int    `json:"native_port"`
}

type connectionPair struct {
	client  net.Conn
	backend net.Conn
}

var activeConnections = struct {
	sync.Mutex
	pairs map[*connectionPair]struct{}
}{pairs: make(map[*connectionPair]struct{})}

func readTarget() (target, error) {
	var value target
	data, err := os.ReadFile(targetPath)
	if err != nil {
		return value, err
	}
	if len(data) > 64*1024 {
		return value, errors.New("target config is too large")
	}
	if err := json.Unmarshal(data, &value); err != nil {
		return value, errors.New("target config is invalid")
	}
	if value.Mode != "local" && value.Mode != "remote" && value.Mode != "disabled" {
		return value, errors.New("target mode is invalid")
	}
	if value.Mode == "remote" {
		if !validHost(value.Host) || value.HTTPPort < 1 || value.HTTPPort > 65535 || value.NativePort < 1 || value.NativePort > 65535 {
			return value, errors.New("remote target is invalid")
		}
	}
	return value, nil
}

func validHost(host string) bool {
	if net.ParseIP(host) != nil {
		return true
	}
	if !hostPattern.MatchString(host) {
		return false
	}
	for _, part := range strings.Split(host, ".") {
		if part == "" || len(part) > 63 || strings.HasPrefix(part, "-") || strings.HasSuffix(part, "-") {
			return false
		}
	}
	return true
}

func configVersion(value target) string {
	data, _ := json.Marshal(value)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func closeActiveConnections() {
	activeConnections.Lock()
	pairs := make([]*connectionPair, 0, len(activeConnections.pairs))
	for pair := range activeConnections.pairs {
		pairs = append(pairs, pair)
	}
	activeConnections.Unlock()
	for _, pair := range pairs {
		_ = pair.client.Close()
		_ = pair.backend.Close()
	}
}

func watchTarget(initial target) {
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	previous := configVersion(initial)
	for range ticker.C {
		value, err := readTarget()
		if err != nil {
			continue
		}
		version := configVersion(value)
		if version != previous {
			closeActiveConnections()
		}
		previous = version
	}
}

func backendAddress(value target, native bool) (string, error) {
	if value.Mode == "local" {
		port := 8123
		if native {
			port = 9000
		}
		return net.JoinHostPort("telemetry-clickhouse", strconv.Itoa(port)), nil
	}
	if value.Mode != "remote" {
		return "", errors.New("Runtime telemetry storage is disabled")
	}
	port := value.HTTPPort
	if native {
		port = value.NativePort
	}
	if value.Host != "runtime-ssh-tunnel" {
		return "", errors.New("remote target must use the internal SSH tunnel")
	}
	return net.JoinHostPort(value.Host, strconv.Itoa(port)), nil
}

func serveNative(listener net.Listener) error {
	for {
		client, err := listener.Accept()
		if err != nil {
			return err
		}
		go func() {
			value, err := readTarget()
			if err != nil || value.Mode == "disabled" {
				_ = client.Close()
				return
			}
			address, err := backendAddress(value, true)
			if err != nil {
				_ = client.Close()
				return
			}
			dialer := &net.Dialer{Timeout: 5 * time.Second}
			backend, err := dialer.Dial("tcp", address)
			if err != nil {
				_ = client.Close()
				return
			}
			pair := &connectionPair{client: client, backend: backend}
			activeConnections.Lock()
			activeConnections.pairs[pair] = struct{}{}
			activeConnections.Unlock()
			done := make(chan struct{}, 2)
			go func() { _, _ = io.Copy(backend, client); done <- struct{}{} }()
			go func() { _, _ = io.Copy(client, backend); done <- struct{}{} }()
			<-done
			_ = client.Close()
			_ = backend.Close()
			activeConnections.Lock()
			delete(activeConnections.pairs, pair)
			activeConnections.Unlock()
		}()
	}
}

func serveHTTP(writer http.ResponseWriter, request *http.Request) {
	value, err := readTarget()
	if err != nil || value.Mode == "disabled" {
		http.Error(writer, "Runtime telemetry storage is unavailable", http.StatusServiceUnavailable)
		return
	}
	address, err := backendAddress(value, false)
	if err != nil {
		http.Error(writer, "Runtime telemetry storage is unavailable", http.StatusServiceUnavailable)
		return
	}
	targetURL := &url.URL{Scheme: "http", Host: address}
	proxy := httputil.NewSingleHostReverseProxy(targetURL)
	proxy.Transport = &http.Transport{
		Proxy:             nil,
		DisableKeepAlives: true,
		ResponseHeaderTimeout: 15 * time.Second,
	}
	proxy.ErrorHandler = func(out http.ResponseWriter, _ *http.Request, _ error) {
		http.Error(out, "Runtime telemetry storage is unavailable", http.StatusBadGateway)
	}
	proxy.ServeHTTP(writer, request)
}

func main() {
	if err := os.MkdirAll(filepath.Dir(targetPath), 0750); err != nil {
		log.Fatal("Runtime target directory is unavailable")
	}
	initial, err := readTarget()
	if err != nil {
		log.Fatal("Runtime target configuration is unavailable")
	}
	go watchTarget(initial)
	native, err := net.Listen("tcp", ":9000")
	if err != nil {
		log.Fatal("native router listener could not start")
	}
	go func() {
		if err := serveNative(native); err != nil {
			log.Printf("native router stopped: %v", err)
		}
	}()
	server := &http.Server{Addr: ":8123", Handler: http.HandlerFunc(serveHTTP), ReadHeaderTimeout: 5 * time.Second}
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal("HTTP router stopped")
	}
}
