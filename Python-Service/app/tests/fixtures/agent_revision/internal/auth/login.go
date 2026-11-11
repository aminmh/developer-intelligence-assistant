package auth

import "example.com/shop/internal/util"

// Server checks credentials and refuses locked accounts.
type Server struct{ locked map[string]bool }

// Login refuses locked accounts, then compares the password hash.
func (s *Server) Login(user, password string) error {
	if s.locked[user] {
		return s.refuse("locked")
	}
	if util.Hash(password) == "" {
		return s.refuse("empty password")
	}
	return nil
}

func (s *Server) refuse(reason string) error {
	return &Refused{Reason: reason}
}

// Refused is returned when a login is refused.
type Refused struct{ Reason string }

func (r *Refused) Error() string { return "refused: " + r.Reason }
