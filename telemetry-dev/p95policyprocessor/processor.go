package p95policyprocessor

import (
	"context"
	"encoding/json"
	"os"
	"sync"
	time "time"

	"go.opentelemetry.io/collector/pdata/ptrace"
)

type policyDocument struct {
	ThresholdsMS map[string]float64 `json:"thresholds_ms"`
}

type policyProcessor struct {
	file       string
	mu         sync.Mutex
	modifiedAt time.Time
	thresholds map[string]float64
}

func (p *policyProcessor) process(_ context.Context, traces ptrace.Traces) (ptrace.Traces, error) {
	thresholds := p.readPolicy()
	resourceSpans := traces.ResourceSpans()
	for i := 0; i < resourceSpans.Len(); i++ {
		resourceSpan := resourceSpans.At(i)
		projectValue, ok := resourceSpan.Resource().Attributes().Get("indexer.project_id")
		if !ok {
			continue
		}
		threshold, hasThreshold := thresholds[projectValue.Str()]
		if !hasThreshold || threshold <= 0 {
			continue
		}
		scopeSpans := resourceSpan.ScopeSpans()
		for j := 0; j < scopeSpans.Len(); j++ {
			spans := scopeSpans.At(j).Spans()
			for k := 0; k < spans.Len(); k++ {
				span := spans.At(k)
				if span.Kind() != ptrace.SpanKindServer || !span.ParentSpanID().IsEmpty() {
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

func (p *policyProcessor) readPolicy() map[string]float64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	stat, err := os.Stat(p.file)
	if err != nil {
		return p.thresholds
	}
	if p.thresholds != nil && stat.ModTime().Equal(p.modifiedAt) {
		return p.thresholds
	}
	content, err := os.ReadFile(p.file)
	if err != nil {
		return p.thresholds
	}
	var document policyDocument
	if err := json.Unmarshal(content, &document); err != nil || document.ThresholdsMS == nil {
		return p.thresholds
	}
	p.thresholds = document.ThresholdsMS
	p.modifiedAt = stat.ModTime()
	return p.thresholds
}
