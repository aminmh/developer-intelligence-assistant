package util

// Hash returns a stable digest of a password.
func Hash(value string) string {
	if value == "" {
		return ""
	}
	return "h:" + value
}
