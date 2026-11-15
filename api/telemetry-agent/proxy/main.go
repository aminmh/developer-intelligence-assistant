// Command docker-api-proxy is the read-only boundary between the Docker socket
// (root-equivalent) and the telemetry agent (the only container with egress).
//
// It is a static, standard-library-only binary on a scratch image: no shell,
// interpreter or package manager runs next to the socket. Beyond allowlisting
// paths it rebuilds every upstream request from validated parts, forces the
// opt-in label filter, and returns only the fields the agent needs, so other
// containers' commands, labels, mounts, addresses and exec events never leave
// this process.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
)

const (
	labelPrefix     = "com.devassistant.telemetry."
	enabledLabel    = labelPrefix + "enabled"
	optIn           = enabledLabel + "=true"
	listenAddr      = ":2375"
	maxConnections  = 8
	upstreamTimeout = 15 * time.Second
	maxListBytes    = 32 << 20
	maxEventLine    = 1 << 20
	maxErrorBody    = 4096
)

// eventActions are lifecycle changes that alter which opted-in containers run.
// Health and exec events are excluded: they are frequent and exec events carry command lines.
var eventActions = []string{"start", "die", "destroy", "rename", "pause", "unpause"}

var (
	apiVersion = regexp.MustCompile(`^v1\.[0-9]{1,3}$`)
	timestamp  = regexp.MustCompile(`^[0-9]{1,12}(\.[0-9]{1,9})?$`)
)

type route int

const (
	routePing route = iota + 1
	routeVersion
	routeContainers
	routeEvents
)

var routeNames = map[route]string{routePing: "ping", routeVersion: "version", routeContainers: "containers", routeEvents: "events"}

// upstreamPath returns the route and the Docker path to request, or ok=false to refuse.
// Client query parameters are never forwarded: only numeric since/until on events survive.
func upstreamPath(method, requestURI string) (route, string, bool) {
	if method != http.MethodGet || !strings.HasPrefix(requestURI, "/") || strings.HasPrefix(requestURI, "//") {
		return 0, "", false
	}
	target, err := url.ParseRequestURI(requestURI)
	if err != nil || target.RawPath != "" || target.Fragment != "" || target.Host != "" {
		return 0, "", false
	}
	switch target.Path {
	case "/_ping":
		return routePing, "/_ping", true
	case "/version":
		return routeVersion, "/version", true
	}
	segments := strings.Split(target.Path, "/")
	if len(segments) < 3 || segments[0] != "" || !apiVersion.MatchString(segments[1]) {
		return 0, "", false
	}
	version, rest := segments[1], segments[2:]
	switch {
	case slices.Equal(rest, []string{"containers", "json"}):
		query := url.Values{"filters": {filters(nil)}}
		return routeContainers, "/" + version + "/containers/json?" + query.Encode(), true
	case slices.Equal(rest, []string{"events"}):
		given, err := url.ParseQuery(target.RawQuery)
		if err != nil {
			return 0, "", false
		}
		query := url.Values{}
		for _, name := range []string{"since", "until"} {
			values, present := given[name]
			if !present {
				continue
			}
			if len(values) != 1 || !timestamp.MatchString(values[0]) {
				return 0, "", false
			}
			query.Set(name, values[0])
		}
		query.Set("filters", filters(map[string][]string{"type": {"container"}, "event": eventActions}))
		return routeEvents, "/" + version + "/events?" + query.Encode(), true
	}
	return 0, "", false
}

func filters(extra map[string][]string) string {
	all := map[string][]string{"label": {optIn}}
	for key, values := range extra {
		all[key] = values
	}
	encoded, _ := json.Marshal(all)
	return string(encoded)
}

func telemetryLabels(labels map[string]any) map[string]string {
	kept := map[string]string{}
	for key, value := range labels {
		if text, isText := value.(string); isText && strings.HasPrefix(key, labelPrefix) {
			kept[key] = text
		}
	}
	return kept
}

type container struct {
	ID      string            `json:"Id"`
	ImageID string            `json:"ImageID"`
	State   string            `json:"State"`
	Labels  map[string]string `json:"Labels"`
}

func trimContainers(body []byte) ([]container, error) {
	var raw []struct {
		ID      string         `json:"Id"`
		ImageID string         `json:"ImageID"`
		State   string         `json:"State"`
		Labels  map[string]any `json:"Labels"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, err
	}
	kept := []container{}
	for _, item := range raw {
		labels := telemetryLabels(item.Labels)
		// Defence in depth: Docker already applied the label filter.
		if labels[enabledLabel] != "true" {
			continue
		}
		kept = append(kept, container{ID: item.ID, ImageID: item.ImageID, State: item.State, Labels: labels})
	}
	return kept, nil
}

type event struct {
	Type     string `json:"Type"`
	Action   string `json:"Action"`
	ID       string `json:"id"`
	Time     int64  `json:"time"`
	TimeNano int64  `json:"timeNano"`
	Actor    struct {
		ID         string            `json:"ID"`
		Attributes map[string]string `json:"Attributes"`
	} `json:"Actor"`
}

func trimEvent(line []byte) (*event, error) {
	var raw struct {
		Type     string `json:"Type"`
		Action   string `json:"Action"`
		ID       string `json:"id"`
		Time     int64  `json:"time"`
		TimeNano int64  `json:"timeNano"`
		Actor    struct {
			ID         string         `json:"ID"`
			Attributes map[string]any `json:"Attributes"`
		} `json:"Actor"`
	}
	if err := json.Unmarshal(line, &raw); err != nil {
		return nil, err
	}
	attributes := telemetryLabels(raw.Actor.Attributes)
	if raw.Type != "container" || !slices.Contains(eventActions, raw.Action) || attributes[enabledLabel] != "true" {
		return nil, nil
	}
	trimmed := &event{Type: raw.Type, Action: raw.Action, ID: raw.ID, Time: raw.Time, TimeNano: raw.TimeNano}
	trimmed.Actor.ID = raw.Actor.ID
	trimmed.Actor.Attributes = attributes
	return trimmed, nil
}

func trimVersion(body []byte) (map[string]string, error) {
	var raw struct {
		Version       string `json:"Version"`
		APIVersion    string `json:"ApiVersion"`
		MinAPIVersion string `json:"MinAPIVersion"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return nil, err
	}
	return map[string]string{"Version": raw.Version, "ApiVersion": raw.APIVersion, "MinAPIVersion": raw.MinAPIVersion}, nil
}

type proxy struct {
	docker *http.Client
	slots  chan struct{}
	log    *slog.Logger
}

func newProxy(socketPath string, logger *slog.Logger) *proxy {
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			var dialer net.Dialer
			return dialer.DialContext(ctx, "unix", socketPath)
		},
		DisableCompression:    true,
		MaxIdleConns:          maxConnections,
		ResponseHeaderTimeout: upstreamTimeout,
	}
	return &proxy{docker: &http.Client{Transport: transport}, slots: make(chan struct{}, maxConnections), log: logger}
}

func (p *proxy) refuse(w http.ResponseWriter, r *http.Request, status int, reason string) {
	// Never log the path or query: they can carry filters and identifiers.
	p.log.Warn("request_refused", "method", r.Method, "reason", reason)
	http.Error(w, http.StatusText(status), status)
}

func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	select {
	case p.slots <- struct{}{}:
		defer func() { <-p.slots }()
	default:
		p.refuse(w, r, http.StatusServiceUnavailable, "busy")
		return
	}
	kind, path, ok := upstreamPath(r.Method, r.RequestURI)
	if !ok {
		p.refuse(w, r, http.StatusForbidden, "denied")
		return
	}
	ctx := r.Context()
	if kind != routeEvents {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, upstreamTimeout)
		defer cancel()
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://docker"+path, nil)
	if err != nil {
		p.fail(w, kind, err, false)
		return
	}
	response, err := p.docker.Do(request)
	if err != nil {
		p.fail(w, kind, err, false)
		return
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || kind == routePing {
		body, _ := io.ReadAll(io.LimitReader(response.Body, maxErrorBody))
		contentType := response.Header.Get("Content-Type")
		if contentType == "" {
			contentType = "text/plain"
		}
		send(w, response.StatusCode, contentType, body)
		return
	}
	if kind == routeEvents {
		if err := streamEvents(w, response.Body); err != nil {
			p.fail(w, kind, err, true)
		}
		return
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, maxListBytes+1))
	if err == nil && len(body) > maxListBytes {
		err = errors.New("response too large")
	}
	var payload any
	if err == nil && kind == routeContainers {
		payload, err = trimContainers(body)
	} else if err == nil {
		payload, err = trimVersion(body)
	}
	if err != nil {
		p.fail(w, kind, err, false)
		return
	}
	encoded, _ := json.Marshal(payload)
	send(w, http.StatusOK, "application/json", encoded)
}

func (p *proxy) fail(w http.ResponseWriter, kind route, err error, started bool) {
	if errors.Is(err, context.Canceled) {
		return // the agent closed the connection
	}
	p.log.Warn("upstream_failed", "route", routeNames[kind], "error_type", errorType(err))
	if !started {
		http.Error(w, http.StatusText(http.StatusBadGateway), http.StatusBadGateway)
	}
}

func errorType(err error) string {
	var netErr net.Error
	var syntaxErr *json.SyntaxError
	switch {
	case errors.As(err, &netErr) && netErr.Timeout():
		return "timeout"
	case errors.As(err, &netErr):
		return "network"
	case errors.As(err, &syntaxErr):
		return "invalid_json"
	default:
		return "other"
	}
}

func send(w http.ResponseWriter, status int, contentType string, body []byte) {
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

func streamEvents(w http.ResponseWriter, body io.Reader) error {
	flusher, _ := w.(http.Flusher)
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.WriteHeader(http.StatusOK)
	if flusher != nil {
		flusher.Flush()
	}
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, 64<<10), maxEventLine)
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		trimmed, err := trimEvent(line)
		if err != nil {
			return err
		}
		if trimmed == nil {
			continue
		}
		encoded, _ := json.Marshal(trimmed)
		if _, err := w.Write(append(encoded, '\n')); err != nil {
			return err
		}
		if flusher != nil {
			flusher.Flush()
		}
	}
	return scanner.Err()
}

// healthcheck lets the scratch image answer a Compose health check without a shell.
func healthcheck() int {
	client := &http.Client{Timeout: 3 * time.Second}
	response, err := client.Get("http://127.0.0.1" + listenAddr + "/_ping")
	if err != nil {
		return 1
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}

func main() {
	check := flag.Bool("healthcheck", false, "probe the running proxy and exit")
	flag.Parse()
	if *check {
		os.Exit(healthcheck())
	}
	logger := slog.New(slog.NewTextHandler(os.Stderr, nil))
	server := &http.Server{
		Addr:              listenAddr,
		Handler:           newProxy("/var/run/docker.sock", logger),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    8 << 10,
		// No Read/WriteTimeout: they would cut the event stream, which stays open while nothing changes.
		// GET requests have no body, so the header timeout bounds what a client can make us read.
		ErrorLog: slog.NewLogLogger(logger.Handler(), slog.LevelWarn),
	}
	logger.Info("listening", "addr", listenAddr)
	if err := server.ListenAndServe(); err != nil {
		logger.Error("server_stopped", "error_type", errorType(err))
		os.Exit(1)
	}
}
