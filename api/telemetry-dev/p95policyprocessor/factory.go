package p95policyprocessor

import (
	"context"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/processor"
	"go.opentelemetry.io/collector/processor/processorhelper"
)

func NewFactory() processor.Factory {
	return processor.NewFactory(
		component.MustNewType("p95policy"),
		createDefaultConfig,
		processor.WithTraces(createTraces, component.StabilityLevelAlpha),
	)
}

func createTraces(
	ctx context.Context,
	set processor.Settings,
	cfg component.Config,
	next consumer.Traces,
) (processor.Traces, error) {
	policyConfig := cfg.(*Config)
	if err := policyConfig.Validate(); err != nil {
		return nil, err
	}
	p := &policyProcessor{file: policyConfig.PolicyFile}
	return processorhelper.NewTraces(
		ctx,
		set,
		cfg,
		next,
		p.process,
		processorhelper.WithCapabilities(consumer.Capabilities{MutatesData: true}),
	)
}
