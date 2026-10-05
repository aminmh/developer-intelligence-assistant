package p95policyprocessor

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	time "time"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

func TestMarksServerRootSlowerThanProjectP95(t *testing.T) {
	path := filepath.Join(t.TempDir(), "policy.json")
	policy, _ := json.Marshal(policyDocument{ThresholdsMS: map[string]float64{"orders": 250}})
	if err := os.WriteFile(path, policy, 0600); err != nil {
		t.Fatal(err)
	}

	traces := ptrace.NewTraces()
	resource := traces.ResourceSpans().AppendEmpty()
	resource.Resource().Attributes().PutStr("indexer.project_id", "orders")
	span := resource.ScopeSpans().AppendEmpty().Spans().AppendEmpty()
	span.SetKind(ptrace.SpanKindServer)
	span.SetStartTimestamp(pcommon.Timestamp(1))
	span.SetEndTimestamp(pcommon.Timestamp(300*time.Millisecond + 1))

	processor := &policyProcessor{file: path}
	if _, err := processor.process(context.Background(), traces); err != nil {
		t.Fatal(err)
	}
	value, ok := span.Attributes().Get("indexer.sampling.slow")
	if !ok || value.Str() != "true" {
		t.Fatalf("expected slow marker, got %v (present=%t)", value, ok)
	}
}
