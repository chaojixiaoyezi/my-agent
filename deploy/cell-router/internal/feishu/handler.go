// Package feishu authenticates webhook bytes and separates durable and synchronous paths.
package feishu

import (
	"bytes"
	"cellrouter/internal/config"
	"cellrouter/internal/directory"
	"cellrouter/internal/store"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

type Ledger interface {
	InsertReceipt(store.Receipt) (*store.Receipt, error)
	GetCard(string, string) (*store.Card, error)
	PutCard(string, string, store.Card) error
}
type Handler struct {
	Config    config.Config
	Directory *directory.Store
	Store     Ledger
	Client    *http.Client
	Notify    func()
	Now       func() time.Time
	mu        sync.Mutex
	cards     map[string]chan struct{}
}

func NewHandler(c config.Config, d *directory.Store, s Ledger, client *http.Client, notify func()) *Handler {
	return &Handler{Config: c, Directory: d, Store: s, Client: client, Notify: notify, Now: time.Now, cards: map[string]chan struct{}{}}
}
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func failure(w http.ResponseWriter, status int, code string) {
	writeJSON(w, status, map[string]string{"error": code})
}
func (h *Handler) credentials(app string) (directory.App, directory.Credentials, bool) {
	return h.Directory.Current().App(app)
}

// LLM: Receipt COMMIT is the acceptance point; notifications are hints sent afterwards.
// 函数用途: 验证平台请求并分流挑战、同步卡片和持久事件。
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) != 3 || parts[0] != "feishu" || parts[2] != "callback" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	d := h.Directory.Current()
	appID := parts[1]
	app, cred, ok := d.App(appID)
	if !ok || app.Routing != "by_owner" {
		http.NotFound(w, r)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, h.Config.MaxBodyBytes)
	raw, e := io.ReadAll(r.Body)
	if e != nil {
		failure(w, http.StatusRequestEntityTooLarge, "BODY_LIMIT")
		return
	}
	p, e := Decode(raw, r.Header, cred, h.Now(), h.Config.TimestampTolerance.Duration)
	if e != nil {
		failure(w, http.StatusForbidden, "FEISHU_INVALID")
		return
	}
	if text(p, "type") == "url_verification" {
		writeJSON(w, http.StatusOK, map[string]string{"challenge": text(p, "challenge")})
		return
	}
	if isCard(p) {
		h.card(w, r, d, appID, cred, p, raw)
		return
	}
	o := ownerOf(p, false)
	_, _, _, known := d.Owner("feishu", appID, o.tenant, o.kind, o.id)
	headers := http.Header{}
	for k, v := range r.Header {
		if !signatureHeader(k) {
			headers[k] = append([]string(nil), v...)
		}
	}
	b, _ := json.Marshal(headers)
	now := h.Now().UnixMilli()
	row := store.Receipt{AppID: appID, EventID: eventKey(p, raw), RawBody: raw, HeadersJSON: string(b), ReceivedAt: now, DirectoryVersion: d.Version, State: "pending", NextAttemptAt: now}
	if !known {
		row.State = "dlq"
		row.LastError = "OWNER_UNKNOWN"
	}
	existing, e := h.Store.InsertReceipt(row)
	if e != nil {
		failure(w, http.StatusServiceUnavailable, "RECEIPT_STORE")
		return
	}
	if existing == nil && known && h.Notify != nil {
		h.Notify()
	}
	writeJSON(w, http.StatusOK, map[string]int{"code": 0})
}

// LLM: Coalesce concurrent card redeliveries without holding the map lock during I/O.
// 函数用途: 在同步截止时间内串行处理相同卡片回调，并复用已持久化结果。
func (h *Handler) card(w http.ResponseWriter, r *http.Request, d *directory.Directory, app string, cred directory.Credentials, p map[string]any, raw []byte) {
	o := ownerOf(p, true)
	_, _, cell, ok := d.Owner("feishu", app, o.tenant, o.kind, o.id)
	if !ok {
		failure(w, http.StatusForbidden, "OWNER_UNKNOWN")
		return
	}
	if cell.State == "frozen" {
		w.Header().Set("Retry-After", strconv.FormatInt(int64(h.Config.ForwardBackoffMax.Seconds()), 10))
		failure(w, http.StatusServiceUnavailable, "CELL_FROZEN")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), h.Config.CardProxyTimeout.Duration)
	defer cancel()
	id := text(object(p, "header"), "event_id")
	if id != "" {
		key := app + "\x00" + id
		for {
			h.mu.Lock()
			prior := h.cards[key]
			if prior == nil {
				h.cards[key] = make(chan struct{})
				h.mu.Unlock()
				break
			}
			h.mu.Unlock()
			select {
			case <-ctx.Done():
				failure(w, http.StatusGatewayTimeout, "CARD_TIMEOUT")
				return
			case <-prior:
			}
		}
		defer func() { h.mu.Lock(); close(h.cards[key]); delete(h.cards, key); h.mu.Unlock() }()
		cached, e := h.Store.GetCard(app, id)
		if e != nil {
			failure(w, http.StatusServiceUnavailable, "CARD_STORE")
			return
		}
		if cached != nil {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(cached.Status)
			_, _ = w.Write(cached.Body)
			return
		}
	}
	status, body, e := post(ctx, h.Client, cell, raw, r.Header, cred, h.Now(), h.Config.MaxResponseBytes)
	if e != nil {
		if errors.Is(e, context.DeadlineExceeded) || ctx.Err() != nil {
			failure(w, http.StatusGatewayTimeout, "CARD_TIMEOUT")
		} else {
			failure(w, http.StatusBadGateway, "CELL_RESPONSE")
		}
		return
	}
	if status >= http.StatusOK && status < http.StatusMultipleChoices && id != "" {
		if e = h.Store.PutCard(app, id, store.Card{Status: status, Body: body, CreatedAt: h.Now().UnixMilli()}); e != nil {
			failure(w, http.StatusServiceUnavailable, "CARD_STORE")
			return
		}
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}

// LLM: The body is immutable; redirect following is disabled by the composition root.
// 函数用途: 用原始正文及新签名向指定 Cell 发起一次有界调用。
func post(ctx context.Context, client *http.Client, cell directory.Cell, raw []byte, headers http.Header, cred directory.Credentials, now time.Time, max int64) (int, []byte, error) {
	req, e := http.NewRequestWithContext(ctx, http.MethodPost, cell.FeishuCallbackURL, bytes.NewReader(raw))
	if e != nil {
		return 0, nil, e
	}
	req.Header = headers.Clone()
	for k := range req.Header {
		if sensitiveHeader(k) {
			delete(req.Header, k)
		}
	}
	req.Header.Set("Content-Type", "application/json")
	if e = resign(req, raw, cred, now); e != nil {
		return 0, nil, e
	}
	resp, e := client.Do(req)
	if e != nil {
		return 0, nil, e
	}
	defer resp.Body.Close()
	body, e := io.ReadAll(io.LimitReader(resp.Body, max+1))
	if e == nil && int64(len(body)) > max {
		e = errors.New("CELL_BODY_LIMIT")
	}
	return resp.StatusCode, body, e
}
func sensitiveHeader(k string) bool {
	k = strings.ToLower(k)
	return k == "authorization" || k == "x-user-id" || k == "x-channel" || k == "x-gateway-token" || k == "x-agent-push-token" || strings.HasPrefix(k, "x-cell-") || k == "connection" || k == "transfer-encoding" || k == "content-length"
}
