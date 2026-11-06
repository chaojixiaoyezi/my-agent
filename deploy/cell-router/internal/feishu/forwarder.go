package feishu

import (
	"cellrouter/internal/config"
	"cellrouter/internal/directory"
	"cellrouter/internal/store"
	"context"
	"encoding/json"
	"log"
	"net/http"
	"strconv"
	"sync"
	"time"
)

type Forwarder struct {
	Config    config.Config
	Directory *directory.Store
	Store     *store.Store
	Client    *http.Client
	Now       func() time.Time
	wake      chan struct{}
}

func NewForwarder(c config.Config, d *directory.Store, s *store.Store, client *http.Client) *Forwarder {
	return &Forwarder{Config: c, Directory: d, Store: s, Client: client, Now: time.Now, wake: make(chan struct{}, c.ForwardWorkers)}
}
func (f *Forwarder) Notify() {
	for i := 0; i < f.Config.ForwardWorkers; i++ {
		select {
		case f.wake <- struct{}{}:
		default:
			return
		}
	}
}
func pause(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

// LLM: No periodic queue polling; inserts/replay/reload wake workers, retries use due time.
// 函数用途: 启动有界转发工人和保留期清理，并在取消时等待全部工人结束。
func (f *Forwarder) Run(ctx context.Context) {
	var wg sync.WaitGroup
	for i := 0; i < f.Config.ForwardWorkers; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); f.worker(ctx) }()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		for pause(ctx, f.Config.ReceiptRetention.Duration) {
			if e := f.Store.PurgeBefore(f.Now().Add(-f.Config.ReceiptRetention.Duration).UnixMilli()); e != nil {
				log.Print("receipt purge: STORE_ERROR")
			}
		}
	}()
	wg.Wait()
}
func (f *Forwarder) worker(ctx context.Context) {
	for ctx.Err() == nil {
		worked, e := f.DeliverOne(ctx)
		if e != nil {
			log.Print("forwarder: STORE_ERROR")
			if !pause(ctx, f.Config.ForwardBackoffInitial.Duration) {
				return
			}
			continue
		}
		if worked {
			continue
		}
		due, ok, e := f.Store.NextDue()
		if e != nil {
			if !pause(ctx, f.Config.ForwardBackoffInitial.Duration) {
				return
			}
			continue
		}
		var timer *time.Timer
		var alarm <-chan time.Time
		if ok {
			delay := time.UnixMilli(due).Sub(f.Now())
			timer = time.NewTimer(delay)
			alarm = timer.C
		}
		select {
		case <-ctx.Done():
			if timer != nil {
				timer.Stop()
			}
			return
		case <-f.wake:
		case <-alarm:
		}
		if timer != nil {
			timer.Stop()
		}
	}
}

// LLM: A persistence failure retries only the state write, never the completed HTTP call.
// 函数用途: 持续提交本次状态决定，进程停止时保留 delivering 供重启恢复。
func (f *Forwarder) persist(ctx context.Context, op func() error) error {
	for {
		e := op()
		if e == nil {
			return nil
		}
		if !pause(ctx, f.Config.ForwardBackoffInitial.Duration) {
			return e
		}
	}
}
func (f *Forwarder) backoff(attempt int) time.Duration {
	d := f.Config.ForwardBackoffInitial.Duration
	max := f.Config.ForwardBackoffMax.Duration
	for i := 1; i < attempt && d < max; i++ {
		if d > max/2 {
			return max
		}
		d *= 2
	}
	if d > max {
		return max
	}
	return d
}

// LLM: Re-read current directory; initial/replayed claims get one attempt without changing receipt age or using a fallback Cell.
// 函数用途: 领取一条回执，重新解析当前归属并以新签名转发或入死信。
func (f *Forwarder) DeliverOne(ctx context.Context) (bool, error) {
	rows, e := f.Store.ClaimDue(f.Now().UnixMilli(), 1)
	if e != nil || len(rows) == 0 {
		return false, e
	}
	r := rows[0]
	dlq := func(reason string) (bool, error) {
		return true, f.persist(ctx, func() error { return f.Store.MarkDLQ(r, reason) })
	}
	if r.Attempts > 1 && f.Now().Sub(time.UnixMilli(r.ReceivedAt)) > f.Config.ForwardRetryHorizon.Duration {
		return dlq("RETRY_HORIZON")
	}
	d := f.Directory.Current()
	_, cred, ok := d.App(r.AppID)
	if !ok {
		return dlq("OWNER_UNKNOWN")
	}
	headers := http.Header{}
	if json.Unmarshal([]byte(r.HeadersJSON), &headers) != nil {
		return dlq("PAYLOAD_INVALID")
	}
	// Persisted bytes were authenticated before acceptance. Re-sign solely to decode with
	// the current app credentials; no expired platform timestamp is reused.
	local := &http.Request{Header: headers.Clone()}
	if e = resign(local, r.RawBody, cred, f.Now()); e != nil {
		return true, e
	}
	p, e := Decode(r.RawBody, local.Header, cred, f.Now(), f.Config.TimestampTolerance.Duration)
	if e != nil {
		return dlq("PAYLOAD_INVALID")
	}
	o := ownerOf(p, false)
	_, _, cell, ok := d.Owner("feishu", r.AppID, o.tenant, o.kind, o.id)
	if !ok {
		return dlq("OWNER_UNKNOWN")
	}
	if e = f.persist(ctx, func() error { return f.Store.SetTarget(r, cell.CellID) }); e != nil {
		return true, e
	}
	reason := "CELL_FROZEN"
	if cell.State != "frozen" {
		attempt, cancel := context.WithTimeout(ctx, f.Config.ForwardTimeout.Duration)
		status, _, err := post(attempt, f.Client, cell, r.RawBody, headers, cred, f.Now(), f.Config.MaxResponseBytes)
		cancel()
		if err == nil && status >= http.StatusOK && status < http.StatusMultipleChoices {
			return true, f.persist(ctx, func() error { return f.Store.MarkDelivered(r) })
		}
		reason = "CELL_RESPONSE"
		if err != nil {
			reason = "CELL_UNAVAILABLE"
		}
	}
	if f.Now().Sub(time.UnixMilli(r.ReceivedAt)) > f.Config.ForwardRetryHorizon.Duration {
		return dlq("RETRY_HORIZON")
	}
	next := f.Now().Add(f.backoff(r.Attempts)).UnixMilli()
	e = f.persist(ctx, func() error { return f.Store.MarkRetry(r, next, reason) })
	f.Notify()
	return true, e
}

// LLM: Register only on the protected admin listener; responses never expose raw events.
// 函数用途: 提供回执查询、死信列表和显式重放操作。
func (f *Forwarder) RegisterAdmin(mux *http.ServeMux) {
	mux.HandleFunc("GET /receipts", func(w http.ResponseWriter, r *http.Request) {
		row, e := f.Store.GetReceipt(r.URL.Query().Get("app_id"), r.URL.Query().Get("event_id"))
		if e != nil {
			failure(w, http.StatusNotFound, "RECEIPT_NOT_FOUND")
			return
		}
		writeJSON(w, http.StatusOK, row)
	})
	mux.HandleFunc("GET /dlq", func(w http.ResponseWriter, r *http.Request) {
		limit := f.Config.AdminListLimit
		if v := r.URL.Query().Get("limit"); v != "" {
			parsed, e := strconv.Atoi(v)
			if e != nil || parsed < 1 || parsed > limit {
				failure(w, http.StatusBadRequest, "LIMIT_INVALID")
				return
			}
			limit = parsed
		}
		rows, e := f.Store.ListDLQ(r.URL.Query().Get("reason"), limit)
		if e != nil {
			failure(w, http.StatusServiceUnavailable, "RECEIPT_STORE")
			return
		}
		writeJSON(w, http.StatusOK, rows)
	})
	mux.HandleFunc("POST /dlq/replay", func(w http.ResponseWriter, r *http.Request) {
		var key struct {
			AppID   string `json:"app_id"`
			EventID string `json:"event_id"`
		}
		r.Body = http.MaxBytesReader(w, r.Body, f.Config.MaxBodyBytes)
		dec := json.NewDecoder(r.Body)
		dec.DisallowUnknownFields()
		if dec.Decode(&key) != nil || key.AppID == "" || key.EventID == "" {
			failure(w, http.StatusBadRequest, "REPLAY_INVALID")
			return
		}
		if e := f.Store.Replay(key.AppID, key.EventID, f.Now().UnixMilli()); e != nil {
			failure(w, http.StatusNotFound, "RECEIPT_NOT_FOUND")
			return
		}
		f.Notify()
		writeJSON(w, http.StatusOK, map[string]int{"code": 0})
	})
}
