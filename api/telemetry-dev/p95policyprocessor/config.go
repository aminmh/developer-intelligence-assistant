package p95policyprocessor

import (
	"fmt"
	"strings"

	"go.opentelemetry.io/collector/component"
)

type Config struct {
	PolicyFile string `mapstructure:"policy_file"`
}

func (cfg *Config) Validate() error {
	if strings.TrimSpace(cfg.PolicyFile) == "" {
		return fmt.Errorf("policy_file must not be empty")
	}
	return nil
}

func createDefaultConfig() component.Config {
	return &Config{PolicyFile: "/var/lib/devassistant/runtime/sampling-policy.json"}
}
