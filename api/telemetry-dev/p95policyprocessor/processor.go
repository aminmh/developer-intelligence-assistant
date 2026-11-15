package p95policyprocessor

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"sync"
	time "time"

	"go.opentelemetry.io/collector/pdata/ptrace"
)

type policyDocument struct {
	// Project p95, used for routes without their own threshold.
	ThresholdsMS map[string]float64 `json:"thresholds_ms"`
	// Per project, the p95 of its busiest routes, keyed "METHOD route". A regression on a fast
	// route never crosses the project-wide p95, so those routes are judged against their own.
	RouteThresholdsMS map[string]map[string]float64 `json:"route_thresholds_ms"`
}

type policyProcessor struct {
	file       string
	mu         sync.Mutex
	modifiedAt time.Time
	policy     *policyDocument
}

func (p *policyProcessor) process(_ context.Context, traces ptrace.Traces) (ptrace.Traces, error) {
	policy := p.readPolicy()
	if policy == nil {
		return traces, nil
	}
	resourceSpans := traces.ResourceSpans()
	for i := 0; i < resourceSpans.Len(); i++ {
		resourceSpan := resourceSpans.At(i)
		projectValue, ok := resourceSpan.Resource().Attributes().Get("indexer.project_id")
		if !ok {
			continue
		}
		project := projectValue.Str()
		projectThreshold := policy.ThresholdsMS[project]
		routeThresholds := policy.RouteThresholdsMS[project]
		scopeSpans := resourceSpan.ScopeSpans()
		for j := 0; j < scopeSpans.Len(); j++ {
			spans := scopeSpans.At(j).Spans()
			for k := 0; k < spans.Len(); k++ {
				span := spans.At(k)
				if span.Kind() != ptrace.SpanKindServer || !span.ParentSpanID().IsEmpty() {
					continue
				}
				threshold := projectThreshold
				if routeThreshold, ok := routeThresholds[routeKey(span)]; ok && routeThreshold > 0 {
					threshold = routeThreshold
				}
				if threshold <= 0 {
					continue
				}
				durationNS := int64(span.EndTimestamp() - span.StartTimestamp())
				if durationNS > int64(threshold*float64(time.Millisecond)) {
					span.Attributes().PutStr("indexer.sampling.slow", "true")
				} else {
					span.Attributes().PutStr("indexer.sampling.slow", "false")
				}
			}
		}
	}
	return traces, nil
}

// routeKey matches the policy's "METHOD route" keys; empty when the span has no route.
func routeKey(span ptrace.Span) string {
	route, ok := span.Attributes().Get("http.route")
	if !ok || route.Str() == "" {
		return ""
	}
	method, _ := span.Attributes().Get("http.request.method")
	return strings.ToUpper(method.Str()) + " " + route.Str()
}

func (p *policyProcessor) readPolicy() *policyDocument {
	p.mu.Lock()
	defer p.mu.Unlock()
	stat, err := os.Stat(p.file)
	if err != nil {
		return p.policy
	}
	if p.policy != nil && stat.ModTime().Equal(p.modifiedAt) {
		return p.policy
	}
	content, err := os.ReadFile(p.file)
	if err != nil {
		return p.policy
	}
	var document policyDocument
	if err := json.Unmarshal(content, &document); err != nil || document.ThresholdsMS == nil {
		return p.policy
	}
	p.policy = &document
	p.modifiedAt = stat.ModTime()
	return p.policy
}
