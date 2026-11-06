package feishu

import (
	"bytes"
	"cellrouter/internal/config"
	"cellrouter/internal/store"
	"cellrouter/internal/testutil"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type rig struct {
	env    *testutil.Env
	ledger *store.Store
	h      *Handler
	f      *Forwarder
	now    time.Time
	config config.Config
}

func setup(t *testing.T, upstream string, encrypted bool) *rig {
	t.Helper()
	env := testutil.New(t, 1, upstream, encrypted)
	s, e := store.Open(filepath.Join(t.TempDir(), "receipts.sqlite"))
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { s.Close() })
	c := config.Defaults()
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	f := NewForwarder(c, env.Directory, s, client)
	h := NewHandler(c, env.Directory, s, client, f.Notify)
	r := &rig{env: env, ledger: s, h: h, f: f, now: time.Unix(1700000000, 0), config: c}
	h.Now = func() time.Time { return r.now }
	f.Now = h.Now
	return r
}
func marshal(t *testing.T, v any) []byte {
	t.Helper()
	b, e := json.Marshal(v)
	if e != nil {
		t.Fatal(e)
	}
	return b
}
func (r *rig) event(t *testing.T, id, owner string) []byte {
	return marshal(t, map[string]any{"schema": "2.0", "header": map[string]any{"event_id": id, "tenant_key": "tenant-example", "token": r.env.Credentials.VerificationToken}, "event": map[string]any{"sender": map[string]any{"sender_id": map[string]string{"open_id": owner}}, "message": map[string]string{"chat_type": "p2p", "message_id": "message-example"}}})
}
func (r *rig) request(t *testing.T, raw []byte) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/feishu/app-example/callback", bytes.NewReader(raw))
	if e := resign(req, raw, r.env.Credentials, r.now); e != nil {
		t.Fatal(e)
	}
	return req
}
func (r *rig) send(t *testing.T, raw []byte) *httptest.ResponseRecorder {
	w := httptest.NewRecorder()
	r.h.ServeHTTP(w, r.request(t, raw))
	return w
}
func encrypt(t *testing.T, key string, raw []byte) []byte {
	t.Helper()
	k := sha256.Sum256([]byte(key))
	b, e := aes.NewCipher(k[:])
	if e != nil {
		t.Fatal(e)
	}
	iv := make([]byte, aes.BlockSize)
	if _, e = rand.Read(iv); e != nil {
		t.Fatal(e)
	}
	pad := aes.BlockSize - len(raw)%aes.BlockSize
	plain := append(append([]byte(nil), raw...), bytes.Repeat([]byte{byte(pad)}, pad)...)
	out := make([]byte, len(plain))
	cipher.NewCBCEncrypter(b, iv).CryptBlocks(out, plain)
	return marshal(t, map[string]string{"encrypt": base64.StdEncoding.EncodeToString(append(iv, out...))})
}

type commitCrashLedger struct{ Ledger }

func (s commitCrashLedger) InsertReceipt(r store.Receipt) (*store.Receipt, error) {
	existing, err := s.Ledger.InsertReceipt(r)
	if err != nil {
		return existing, err
	}
	panic("crash-after-commit")
}

type ackObserver struct {
	*httptest.ResponseRecorder
	wroteHeader bool
}

func (w *ackObserver) WriteHeader(status int) {
	w.wroteHeader = true
	w.ResponseRecorder.WriteHeader(status)
}

func TestDurableAcceptanceDuplicatesAndCrashPoints(t *testing.T) {
	var calls atomic.Int32
	var mu sync.Mutex
	distinct := map[string]bool{}
	cell := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		b, _ := io.ReadAll(req.Body)
		var p map[string]any
		_ = json.Unmarshal(b, &p)
		mu.Lock()
		distinct[eventKey(p, b)] = true
		mu.Unlock()
		calls.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	defer cell.Close()
	r := setup(t, cell.URL, false)
	raw := r.event(t, "event-example", "ou_example_00")
	// Exit at the exact point after COMMIT and before any ACK header is written.
	r.h.Store = commitCrashLedger{Ledger: r.ledger}
	observer := &ackObserver{ResponseRecorder: httptest.NewRecorder()}
	func() {
		defer func() {
			if recovered := recover(); recovered != "crash-after-commit" {
				t.Fatalf("unexpected crash point: %v", recovered)
			}
		}()
		r.h.ServeHTTP(observer, r.request(t, raw))
		t.Fatal("crash point not reached")
	}()
	if observer.wroteHeader || observer.Body.Len() != 0 {
		t.Fatal("ACK written before crash point")
	}
	r.h.Store = r.ledger
	if row, e := r.ledger.GetReceipt("app-example", "event-example"); e != nil || row.State != "pending" {
		t.Fatal(row, e)
	}
	// On restart the durable row, not the lost notification or ACK, drives delivery.
	if e := r.ledger.ResetDelivering(); e != nil {
		t.Fatal(e)
	}
	if w := r.send(t, raw); w.Code != 200 {
		t.Fatal(w.Code)
	}
	if _, e := r.f.DeliverOne(context.Background()); e != nil {
		t.Fatal(e)
	}
	if _, e := r.f.DeliverOne(context.Background()); e != nil {
		t.Fatal(e)
	}
	if calls.Load() != 1 {
		t.Fatal("duplicate delivery", calls.Load())
	}
	// Simulate process exit after the Cell returns 2xx, before MarkDelivered commits.
	raw = r.event(t, "crash-example", "ou_example_00")
	if w := r.send(t, raw); w.Code != 200 {
		t.Fatal(w.Code)
	}
	rows, e := r.ledger.ClaimDue(r.now.UnixMilli(), 1)
	if e != nil || len(rows) != 1 {
		t.Fatal(rows, e)
	}
	_, cred, _ := r.env.Directory.Current().App("app-example")
	_, _, target, _ := r.env.Directory.Current().Owner("feishu", "app-example", "tenant-example", "user", "ou_example_00")
	if _, _, e = post(context.Background(), r.h.Client, target, raw, http.Header{}, cred, r.now, r.config.MaxResponseBytes); e != nil {
		t.Fatal(e)
	}
	if e = r.ledger.ResetDelivering(); e != nil {
		t.Fatal(e)
	}
	if _, e = r.f.DeliverOne(context.Background()); e != nil {
		t.Fatal(e)
	}
	mu.Lock()
	n := len(distinct)
	mu.Unlock()
	if calls.Load() != 3 || n != 2 {
		t.Fatalf("calls=%d distinct=%d", calls.Load(), n)
	}
}

type failingLedger struct {
	Ledger
	inserted atomic.Int32
}

func (s *failingLedger) InsertReceipt(store.Receipt) (*store.Receipt, error) {
	s.inserted.Add(1)
	return nil, errors.New("store unavailable")
}
func TestStoreFailureNeverACKsOrNotifies(t *testing.T) {
	r := setup(t, "http://cell-a.example.internal:8421", false)
	broken := &failingLedger{Ledger: r.ledger}
	r.h.Store = broken
	notified := 0
	r.h.Notify = func() { notified++ }
	w := r.send(t, r.event(t, "event-example", "ou_example_00"))
	if w.Code != 503 || notified != 0 || broken.inserted.Load() != 1 {
		t.Fatal(w.Code, notified)
	}
	if rows, e := r.ledger.ClaimDue(r.now.UnixMilli(), 1); e != nil || len(rows) != 0 {
		t.Fatal(rows, e)
	}
}

type blockingLedger struct {
	Ledger
	entered, release chan struct{}
}

func (s *blockingLedger) InsertReceipt(r store.Receipt) (*store.Receipt, error) {
	close(s.entered)
	<-s.release
	return s.Ledger.InsertReceipt(r)
}
func TestACKWaitsForCommit(t *testing.T) {
	r := setup(t, "http://cell-a.example.internal:8421", false)
	blocked := &blockingLedger{Ledger: r.ledger, entered: make(chan struct{}), release: make(chan struct{})}
	r.h.Store = blocked
	done := make(chan struct{})
	w := httptest.NewRecorder()
	req := r.request(t, r.event(t, "event-example", "ou_example_00"))
	go func() { r.h.ServeHTTP(w, req); close(done) }()
	<-blocked.entered
	select {
	case <-done:
		t.Fatal("ACK before commit")
	default:
	}
	if _, e := r.ledger.GetReceipt("app-example", "event-example"); e != store.ErrNotFound {
		t.Fatal(e)
	}
	close(blocked.release)
	<-done
	if w.Code != 200 {
		t.Fatal(w.Code)
	}
}
func TestUnknownOwnerReplayAfterReload(t *testing.T) {
	var calls atomic.Int32
	cell := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1) }))
	defer cell.Close()
	r := setup(t, cell.URL, false)
	if w := r.send(t, r.event(t, "event-example", "ou_new_example")); w.Code != 200 {
		t.Fatal(w.Code)
	}
	row, e := r.ledger.GetReceipt("app-example", "event-example")
	if e != nil || row.State != "dlq" || row.LastError != "OWNER_UNKNOWN" || calls.Load() != 0 {
		t.Fatal(row, e)
	}
	if done, e := r.f.DeliverOne(context.Background()); e != nil || done {
		t.Fatal(done, e)
	}
	owner := r.env.Seed.Owners[0]
	owner.LocalOwnerID = "ou_new_example"
	owner.ProviderOwnerID = owner.LocalOwnerID
	r.env.Seed.Owners = append(r.env.Seed.Owners, owner)
	r.env.Seed.Version++
	r.env.Save(t)
	if e = r.env.Directory.Reload(); e != nil {
		t.Fatal(e)
	}
	mux := http.NewServeMux()
	r.f.RegisterAdmin(mux)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest("POST", "/dlq/replay", bytes.NewBufferString(`{"app_id":"app-example","event_id":"event-example"}`)))
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body)
	}
	if _, e = r.f.DeliverOne(context.Background()); e != nil {
		t.Fatal(e)
	}
	row, e = r.ledger.GetReceipt("app-example", "event-example")
	if e != nil || row.State != "delivered" || calls.Load() != 1 {
		t.Fatal(row, e, calls.Load())
	}
}
func TestRetryBackoffAndHorizon(t *testing.T) {
	cell := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(503) }))
	defer cell.Close()
	r := setup(t, cell.URL, false)
	if w := r.send(t, r.event(t, "event-example", "ou_example_00")); w.Code != 200 {
		t.Fatal(w.Code)
	}
	var last time.Duration
	for i := 0; i < 4; i++ {
		before := r.now
		if done, e := r.f.DeliverOne(context.Background()); e != nil || !done {
			t.Fatal(done, e)
		}
		row, e := r.ledger.GetReceipt("app-example", "event-example")
		if e != nil {
			t.Fatal(e)
		}
		delay := time.UnixMilli(row.NextAttemptAt).Sub(before)
		if delay <= last {
			t.Fatal("delay did not grow", delay, last)
		}
		last = delay
		r.now = time.UnixMilli(row.NextAttemptAt)
	}
	r.now = r.now.Add(r.config.ForwardRetryHorizon.Duration)
	if _, e := r.f.DeliverOne(context.Background()); e != nil {
		t.Fatal(e)
	}
	row, e := r.ledger.GetReceipt("app-example", "event-example")
	if e != nil || row.State != "dlq" || row.LastError != "RETRY_HORIZON" {
		t.Fatal(row, e)
	}
}
func TestResignedBytesAndReplayWindow(t *testing.T) {
	var r *rig
	seen := map[string]bool{}
	var timestamps []string
	var first []byte
	count := 0
	cell := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		raw, _ := io.ReadAll(req.Body)
		if _, e := Decode(raw, req.Header, r.env.Credentials, r.now, r.config.TimestampTolerance.Duration); e != nil {
			t.Error(e)
			w.WriteHeader(403)
			return
		}
		key := one(req.Header, "X-Lark-Request-Timestamp") + one(req.Header, "X-Lark-Request-Nonce") + one(req.Header, "X-Lark-Signature")
		if seen[key] {
			t.Error("reused signed request")
			w.WriteHeader(403)
			return
		}
		seen[key] = true
		timestamps = append(timestamps, one(req.Header, "X-Lark-Request-Timestamp"))
		if first == nil {
			first = raw
		} else if !bytes.Equal(first, raw) {
			t.Error("wire body changed")
		}
		count++
		if count < 3 {
			w.WriteHeader(503)
		}
	}))
	defer cell.Close()
	r = setup(t, cell.URL, true)
	raw := encrypt(t, r.env.Credentials.EncryptKey, r.event(t, "event-example", "ou_example_00"))
	if w := r.send(t, raw); w.Code != 200 {
		t.Fatal(w.Code, w.Body)
	}
	for i := 0; i < 3; i++ {
		if _, e := r.f.DeliverOne(context.Background()); e != nil {
			t.Fatal(e)
		}
		row, e := r.ledger.GetReceipt("app-example", "event-example")
		if e != nil {
			t.Fatal(e)
		}
		r.now = time.UnixMilli(row.NextAttemptAt)
	}
	if count != 3 || len(seen) != 3 || timestamps[0] == timestamps[1] || timestamps[1] == timestamps[2] || !bytes.Equal(first, raw) {
		t.Fatal(count, timestamps)
	}
}
func TestChallengeAndInvalidSignatures(t *testing.T) {
	for _, encrypted := range []bool{false, true} {
		t.Run(fmt.Sprint(encrypted), func(t *testing.T) {
			r := setup(t, "http://cell-a.example.internal:8421", encrypted)
			raw := marshal(t, map[string]string{"type": "url_verification", "challenge": "challenge-example", "token": r.env.Credentials.VerificationToken})
			if encrypted {
				raw = encrypt(t, r.env.Credentials.EncryptKey, raw)
			}
			w := r.send(t, raw)
			if w.Code != 200 || w.Body.String() != "{\"challenge\":\"challenge-example\"}\n" {
				t.Fatal(w.Code, w.Body)
			}
			if _, e := r.ledger.GetReceipt("app-example", eventKey(nil, raw)); e != store.ErrNotFound {
				t.Fatal(e)
			}
			if encrypted {
				req := r.request(t, raw)
				req.Body = io.NopCloser(bytes.NewReader(append([]byte(" "), raw...)))
				w = httptest.NewRecorder()
				r.h.ServeHTTP(w, req)
				if w.Code != 403 {
					t.Fatal(w.Code)
				}
				req = r.request(t, raw)
				req.Header.Set("X-Lark-Request-Timestamp", "1")
				w = httptest.NewRecorder()
				r.h.ServeHTTP(w, req)
				if w.Code != 403 {
					t.Fatal(w.Code)
				}
			} else {
				if w := r.send(t, []byte(`{"token":""}`)); w.Code != 403 {
					t.Fatal(w.Code)
				}
			}
		})
	}
}
func TestCardExactResultCacheAndTimeout(t *testing.T) {
	for _, slow := range []bool{false, true} {
		t.Run(fmt.Sprint(slow), func(t *testing.T) {
			var calls atomic.Int32
			body := []byte("{ \"toast\":{\"type\":\"success\",\"content\":\"example\"}, \"card\":{\"type\":\"raw\",\"data\":{}} }")
			cell := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				calls.Add(1)
				if slow {
					time.Sleep(40 * time.Millisecond)
				}
				w.WriteHeader(201)
				_, _ = w.Write(body)
			}))
			defer cell.Close()
			r := setup(t, cell.URL, true)
			r.h.Config.CardProxyTimeout = config.Duration{Duration: 10 * time.Millisecond}
			raw := encrypt(t, r.env.Credentials.EncryptKey, marshal(t, map[string]any{"header": map[string]string{"tenant_key": "tenant-example", "event_id": "card-example"}, "event": map[string]any{"operator": map[string]string{"open_id": "ou_example_00"}, "action": map[string]any{"value": map[string]string{"kind": "password-card-example"}}}}))
			w := r.send(t, raw)
			if slow {
				if w.Code != 504 {
					t.Fatal(w.Code, w.Body)
				}
				cached, e := r.ledger.GetCard("app-example", "card-example")
				if e != nil || cached != nil {
					t.Fatal(cached, e)
				}
				return
			}
			if w.Code != 201 || !bytes.Equal(w.Body.Bytes(), body) {
				t.Fatal(w.Code, w.Body)
			}
			w = r.send(t, raw)
			if w.Code != 201 || !bytes.Equal(w.Body.Bytes(), body) || calls.Load() != 1 {
				t.Fatal(w.Code, w.Body, calls.Load())
			}
		})
	}
}

func TestSignatureFixture(t *testing.T) {
	b, e := os.ReadFile("../../testdata/signature.json")
	if e != nil {
		t.Fatal(e)
	}
	var f struct{ Timestamp, Nonce, Body, Expected string }
	if e = json.Unmarshal(b, &f); e != nil {
		t.Fatal(e)
	}
	if got := Signature(f.Timestamp, f.Nonce, "", []byte(f.Body)); got != f.Expected {
		t.Fatalf("signature fixture: %s", got)
	}
}

func TestCredentialReloadPreservesCommittedEvent(t *testing.T) {
	for _, encrypted := range []bool{false, true} {
		t.Run(fmt.Sprint(encrypted), func(t *testing.T) {
			var r *rig
			var calls atomic.Int32
			cell := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
				raw, _ := io.ReadAll(req.Body)
				if _, e := Decode(raw, req.Header, r.env.Credentials, r.now, r.config.TimestampTolerance.Duration); e != nil {
					t.Error(e)
					w.WriteHeader(403)
					return
				}
				calls.Add(1)
			}))
			defer cell.Close()
			r = setup(t, cell.URL, encrypted)
			raw := r.event(t, "event-example", "ou_example_00")
			if encrypted {
				raw = encrypt(t, r.env.Credentials.EncryptKey, raw)
			}
			if w := r.send(t, raw); w.Code != 200 {
				t.Fatal(w.Code)
			}
			snapshot := r.env.Directory.Current()
			name := "verification_token"
			if encrypted {
				name = "encrypt_key"
			}
			if e := os.WriteFile(filepath.Join(r.env.Seed.Apps[0].CredentialRef, name), []byte(testutil.Random(t)), 0600); e != nil {
				t.Fatal(e)
			}
			if e := r.env.Directory.Reload(); e == nil || r.env.Directory.Current() != snapshot {
				t.Fatal("credential replacement published", e)
			}
			if _, e := r.f.DeliverOne(context.Background()); e != nil {
				t.Fatal(e)
			}
			row, e := r.ledger.GetReceipt("app-example", "event-example")
			if e != nil || row.State != "delivered" || calls.Load() != 1 {
				t.Fatal(row, e, calls.Load())
			}
		})
	}
}
