package main

import (
	"bufio"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestAllowsOnlyReadInventoryEventsAndHealth(t *testing.T) {
	allowed := map[string]route{
		"/_ping":                 routePing,
		"/version":               routeVersion,
		"/v1.44/containers/json": routeContainers,
		"/v1.41/events?since=10": routeEvents,
	}
	for path, want := range allowed {
		if kind, _, ok := upstreamPath("GET", path); !ok || kind != want {
			t.Errorf("GET %s: got %v %v, want %v", path, kind, ok, want)
		}
	}
	denied := [][2]string{
		{"POST", "/v1.41/containers/create"},
		{"POST", "/v1.41/events"},
		{"HEAD", "/_ping"},
		{"GET", "/v1.41/containers/abc/json"},
		{"GET", "/v1.41/containers/abc/logs"},
		{"GET", "/v1.41/containers/abc/archive"},
		{"GET", "/v1.41/containers/abc/exec"},
		{"GET", "/v1.41/images/json"},
		{"GET", "/vX/events"},
		{"GET", "/v2.0/events"},
		{"GET", "/v1.41/events/"},
		{"GET", "//v1.41/events"},
		{"GET", "/v1.41//events"},
		{"GET", "/v1.41/containers/json/../abc/logs"},
		{"GET", "/v1.41/events%2F..%2Fimages"},
		{"GET", "/v1.41%2Fevents"},
		{"GET", "http://docker/v1.41/events"},
		{"GET", "*"},
		{"GET", "/v1.41/events?since=1;rm"},
		{"GET", "/v1.41/events?since=1&since=2"},
		{"GET", "/v1.41/events?since=-1"},
	}
	for _, item := range denied {
		if _, _, ok := upstreamPath(item[0], item[1]); ok {
			t.Errorf("%s %s should be refused", item[0], item[1])
		}
	}
}

func TestRebuildsQueriesAndForcesTheOptInFilter(t *testing.T) {
	_, path, _ := upstreamPath("GET", "/v1.44/containers/json?all=1&size=1&filters=%7B%7D")
	query := mustQuery(t, path)
	if len(query) != 1 {
		t.Fatalf("containers query kept client parameters: %v", query)
	}
	var containerFilters map[string][]string
	mustJSON(t, query.Get("filters"), &containerFilters)
	if !slices.Equal(containerFilters["label"], []string{optIn}) || len(containerFilters) != 1 {
		t.Fatalf("containers filter = %v", containerFilters)
	}

	_, path, _ = upstreamPath("GET", "/v1.44/events?since=10&until=20.5&filters=%7B%7D&x=1")
	query = mustQuery(t, path)
	if query.Get("since") != "10" || query.Get("until") != "20.5" || len(query) != 3 {
		t.Fatalf("events query = %v", query)
	}
	var eventFilters map[string][]string
	mustJSON(t, query.Get("filters"), &eventFilters)
	if !slices.Equal(eventFilters["label"], []string{optIn}) || !slices.Equal(eventFilters["type"], []string{"container"}) {
		t.Fatalf("events filter = %v", eventFilters)
	}
	for _, noisy := range []string{"exec_start", "exec_create", "health_status"} {
		if slices.Contains(eventFilters["event"], noisy) {
			t.Fatalf("events filter allows %s", noisy)
		}
	}
}

func TestReturnsOnlyTelemetryFields(t *testing.T) {
	containers, err := trimContainers([]byte(`[
		{"Id":"a","ImageID":"sha256:b","State":"running","Command":"server --password=secret","Mounts":[{"Source":"/etc"}],
		 "Labels":{"com.devassistant.telemetry.enabled":"true","com.example.secret":"x"}},
		{"Id":"c","Labels":{"other":"1"}}]`))
	if err != nil {
		t.Fatal(err)
	}
	encoded, _ := json.Marshal(containers)
	if string(encoded) != `[{"Id":"a","ImageID":"sha256:b","State":"running","Labels":{"com.devassistant.telemetry.enabled":"true"}}]` {
		t.Fatalf("containers = %s", encoded)
	}

	started, _ := trimEvent([]byte(`{"Type":"container","Action":"start","id":"a","time":5,"timeNano":5000,
		"Actor":{"ID":"a","Attributes":{"com.devassistant.telemetry.enabled":"true","image":"x","name":"db"}}}`))
	if started == nil || len(started.Actor.Attributes) != 1 {
		t.Fatalf("start event = %+v", started)
	}
	for _, line := range []string{
		`{"Type":"container","Action":"exec_start: sh -c 'mysql -psecret'","Actor":{"Attributes":{"com.devassistant.telemetry.enabled":"true"}}}`,
		`{"Type":"container","Action":"start","Actor":{"Attributes":{}}}`,
		`{"Type":"network","Action":"start","Actor":{"Attributes":{"com.devassistant.telemetry.enabled":"true"}}}`,
	} {
		if trimmed, err := trimEvent([]byte(line)); err != nil || trimmed != nil {
			t.Errorf("event should be dropped: %s", line)
		}
	}

	version, _ := trimVersion([]byte(`{"Version":"29.0","ApiVersion":"1.52","MinAPIVersion":"1.44","KernelVersion":"6"}`))
	if len(version) != 3 || version["MinAPIVersion"] != "1.44" {
		t.Fatalf("version = %v", version)
	}
}

// TestProxyAgainstFakeDocker runs the real handler against a Docker stand-in on a Unix socket.
func TestProxyAgainstFakeDocker(t *testing.T) {
	var seen []string
	docker := http.NewServeMux()
	docker.HandleFunc("/v1.44/containers/json", func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.URL.RawQuery)
		io.WriteString(w, `[{"Id":"a","ImageID":"sha256:b","State":"running","Command":"secret","Labels":{"com.devassistant.telemetry.enabled":"true","x":"y"}}]`)
	})
	docker.HandleFunc("/v1.44/events", func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.URL.RawQuery)
		flusher := w.(http.Flusher)
		for _, line := range []string{
			`{"Type":"container","Action":"exec_start: env","Actor":{"Attributes":{"com.devassistant.telemetry.enabled":"true"}}}`,
			`{"Type":"container","Action":"start","id":"a","time":7,"Actor":{"ID":"a","Attributes":{"com.devassistant.telemetry.enabled":"true","name":"db"}}}`,
		} {
			io.WriteString(w, line+"\n")
			flusher.Flush()
		}
	})
	directory, err := os.MkdirTemp("", "dp")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(directory)
	socketPath := filepath.Join(directory, "d.sock")
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		t.Fatal(err)
	}
	upstream := &httptest.Server{Listener: listener, Config: &http.Server{Handler: docker}}
	upstream.Start()
	defer upstream.Close()

	front := httptest.NewServer(newProxy(socketPath, slog.New(slog.NewTextHandler(io.Discard, nil))))
	defer front.Close()

	body := get(t, front.URL+"/v1.44/containers/json?all=1&size=1", http.StatusOK)
	if strings.Contains(body, "secret") || strings.Contains(body, `"x"`) {
		t.Fatalf("container list leaked fields: %s", body)
	}
	if strings.Contains(seen[0], "size") || strings.Contains(seen[0], "all") {
		t.Fatalf("client parameters reached Docker: %s", seen[0])
	}

	response, err := http.Get(front.URL + "/v1.44/events?since=5")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	lines := []string{}
	scanner := bufio.NewScanner(response.Body)
	for scanner.Scan() {
		lines = append(lines, scanner.Text())
	}
	if len(lines) != 1 || !strings.Contains(lines[0], `"Action":"start"`) || strings.Contains(lines[0], `"name"`) {
		t.Fatalf("events = %v", lines)
	}

	get(t, front.URL+"/v1.44/containers/a/logs", http.StatusForbidden)
	request, _ := http.NewRequest(http.MethodPost, front.URL+"/v1.44/containers/create", nil)
	posted, err := http.DefaultClient.Do(request)
	if err != nil || posted.StatusCode != http.StatusForbidden {
		t.Fatalf("POST was not refused: %v %v", posted, err)
	}
	posted.Body.Close()
}

func get(t *testing.T, target string, status int) string {
	t.Helper()
	response, err := http.Get(target)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, _ := io.ReadAll(response.Body)
	if response.StatusCode != status {
		t.Fatalf("GET %s: status %d, want %d", target, response.StatusCode, status)
	}
	return string(body)
}

func mustQuery(t *testing.T, path string) url.Values {
	t.Helper()
	parsed, err := url.Parse(path)
	if err != nil {
		t.Fatal(err)
	}
	return parsed.Query()
}

func mustJSON(t *testing.T, text string, target any) {
	t.Helper()
	if err := json.Unmarshal([]byte(text), target); err != nil {
		t.Fatal(err)
	}
}
