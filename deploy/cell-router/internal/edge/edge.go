// Package edge resolves personal credentials into exactly one Cell identity.
package edge

import (
	"cellrouter/internal/config"
	"cellrouter/internal/directory"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"strings"
)

type Handler struct {
	Config    config.Config
	Directory *directory.Store
	Transport http.RoundTripper
}

func credential(h http.Header) string {
	var values []string
	for k, v := range h {
		if strings.EqualFold(k, "X-Gateway-Token") {
			values = append(values, v...)
		}
	}
	if len(values) > 1 {
		for _, value := range values[1:] {
			if value != values[0] {
				return ""
			}
		}
	}
	if len(values) > 0 && values[0] != "" {
		return values[0]
	}
	values = nil
	for k, v := range h {
		if strings.EqualFold(k, "Authorization") {
			values = append(values, v...)
		}
	}
	if len(values) != 1 {
		return ""
	}
	parts := strings.Fields(values[0])
	if len(parts) == 2 && strings.EqualFold(parts[0], "Bearer") {
		return parts[1]
	}
	return ""
}
func removeIdentity(h http.Header) {
	for k := range h {
		name := strings.ToLower(k)
		switch name {
		case "x-user-id", "x-channel", "x-gateway-token", "x-agent-push-token", "authorization":
			delete(h, k)
		default:
			if strings.HasPrefix(name, "x-cell-") {
				delete(h, k)
			}
		}
	}
}
func identity(h http.Header, p directory.Principal, token string) {
	removeIdentity(h)
	h.Set("X-Gateway-Token", token)
	h.Set("X-User-Id", p.LocalOwnerID)
	h.Set("X-Channel", p.PrincipalKind)
}

// LLM: Rewrite operates on the outgoing header map after ReverseProxy strips hop headers.
// 函数用途: 只信任目录身份，清除来路身份字段并无缓冲代理 Cell 流。
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	token := credential(r.Header)
	sum := sha256.Sum256([]byte(token))
	d := h.Directory.Current()
	p, _, cell, ok := d.Principal(hex.EncodeToString(sum[:]))
	if token == "" || !ok {
		http.Error(w, "UNAUTHORIZED", http.StatusUnauthorized)
		return
	}
	if cell.State == "frozen" {
		w.Header().Set("Retry-After", strconv.FormatInt(int64(h.Config.ForwardBackoffMax.Seconds()), 10))
		http.Error(w, "CELL_FROZEN", http.StatusServiceUnavailable)
		return
	}
	cellToken := d.CellToken(cell.CellID)
	if r.URL.Path == "/authz" {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		identity(w.Header(), p, cellToken)
		w.Header().Set("X-Cell-Upstream", cell.BaseURL)
		w.WriteHeader(http.StatusOK)
		return
	}
	u, e := url.Parse(cell.BaseURL)
	if e != nil {
		http.Error(w, "CELL_CONFIG", http.StatusBadGateway)
		return
	}
	proxy := httputil.ReverseProxy{
		Rewrite:       func(pr *httputil.ProxyRequest) { pr.SetURL(u); identity(pr.Out.Header, p, cellToken) },
		FlushInterval: -1, Transport: h.Transport,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, e error) {
			http.Error(w, "CELL_UNAVAILABLE", http.StatusBadGateway)
		},
	}
	proxy.ServeHTTP(w, r)
}
