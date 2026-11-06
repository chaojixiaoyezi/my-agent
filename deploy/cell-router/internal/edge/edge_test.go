package edge

import (
	"bufio"
	"cellrouter/internal/config"
	"cellrouter/internal/testutil"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestIdentityRewriteAndAuthorization(t *testing.T) {
	var calls atomic.Int32
	var received http.Header
	cell := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received = r.Header.Clone()
		calls.Add(1)
		w.WriteHeader(202)
	}))
	defer cell.Close()
	e := testutil.New(t, 1, cell.URL, false)
	h := &Handler{Config: config.Defaults(), Directory: e.Directory}
	req := httptest.NewRequest("POST", "/client/business/send", nil)
	for _, name := range []string{"X-User-Id", "X-Channel", "X-Agent-Push-Token", "X-Cell-Id", "Authorization"} {
		req.Header[name] = []string{"client-value-a"}
		req.Header[strings.ToLower(name)] = []string{"client-value-b"}
	}
	req.Header["X-Gateway-Token"] = []string{e.Token}
	req.Header["x-gateway-token"] = []string{e.Token}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != 202 || calls.Load() != 1 {
		t.Fatal(w.Code, calls.Load())
	}
	for name, want := range map[string]string{"X-User-Id": "ou_example_00", "X-Channel": "feishu", "X-Gateway-Token": e.CellToken} {
		if values := received.Values(name); len(values) != 1 || values[0] != want {
			t.Fatalf("%s=%v", name, values)
		}
	}
	for _, name := range []string{"X-Agent-Push-Token", "X-Cell-Id", "Authorization"} {
		if len(received.Values(name)) != 0 {
			t.Fatal(name, received.Values(name))
		}
	}
	for _, token := range []string{"", testutil.Random(t)} {
		req = httptest.NewRequest("GET", "/client/business/send", nil)
		req.Header.Set("X-Gateway-Token", token)
		w = httptest.NewRecorder()
		h.ServeHTTP(w, req)
		if w.Code != 401 || calls.Load() != 1 {
			t.Fatal(w.Code, calls.Load())
		}
	}
	req = httptest.NewRequest("GET", "/authz", nil)
	req.Header.Set("Authorization", "Bearer "+e.Token)
	w = httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != 200 || w.Header().Get("X-User-Id") != "ou_example_00" || w.Header().Get("X-Channel") != "feishu" || w.Header().Get("X-Gateway-Token") != e.CellToken || w.Header().Get("X-Cell-Upstream") != cell.URL || calls.Load() != 1 {
		t.Fatal(w.Code, w.Header())
	}
	e.Seed.Cells[0].State = "frozen"
	e.Save(t)
	if err := e.Directory.Reload(); err != nil {
		t.Fatal(err)
	}
	w = httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != 503 || w.Header().Get("Retry-After") != "60" || calls.Load() != 1 {
		t.Fatal(w.Code, w.Header())
	}
	e.Seed.Cells[0].State = "draining"
	e.Save(t)
	if err := e.Directory.Reload(); err != nil {
		t.Fatal(err)
	}
	req.URL.Path = "/client/business/send"
	w = httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != 202 || calls.Load() != 2 {
		t.Fatal(w.Code, calls.Load())
	}
}
func TestHeaderRewriteAllClientValues(t *testing.T) {
	e := testutil.New(t, 1, "http://cell-a.example.internal", false)
	p, _, _, ok := e.Directory.Current().Principal(e.Seed.Principals[0].TokenSHA256)
	if !ok {
		t.Fatal("principal missing")
	}
	headers := http.Header{}
	for _, name := range []string{"X-User-Id", "X-Channel", "X-Gateway-Token", "X-Agent-Push-Token", "X-Cell-Extra", "Authorization"} {
		headers[name] = []string{"a", "b"}
		headers[strings.ToLower(name)] = []string{"c", "d"}
	}
	identity(headers, p, e.CellToken)
	if len(headers) != 3 || len(headers.Values("X-Gateway-Token")) != 1 || headers.Get("X-Gateway-Token") != e.CellToken || headers.Get("X-User-Id") != "ou_example_00" || headers.Get("X-Channel") != "feishu" {
		t.Fatal("identity header set differs")
	}
}
func TestNDJSONFlushesBeforeUpstreamFinishes(t *testing.T) {
	finished := make(chan struct{})
	cell := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer close(finished)
		w.Header().Set("Content-Type", "application/x-ndjson")
		for i := 0; i < 3; i++ {
			fmt.Fprintf(w, "{\"line\":%d}\n", i)
			w.(http.Flusher).Flush()
			time.Sleep(50 * time.Millisecond)
		}
	}))
	defer cell.Close()
	e := testutil.New(t, 1, cell.URL, false)
	proxy := httptest.NewServer(&Handler{Config: config.Defaults(), Directory: e.Directory})
	defer proxy.Close()
	req, _ := http.NewRequest("GET", proxy.URL+"/client/business/stream", nil)
	req.Header.Set("X-Gateway-Token", e.Token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	scanner := bufio.NewScanner(resp.Body)
	if !scanner.Scan() || scanner.Text() != `{"line":0}` {
		t.Fatal(scanner.Text(), scanner.Err())
	}
	select {
	case <-finished:
		t.Fatal("first line buffered until upstream ended")
	default:
	}
	for i := 1; i < 3; i++ {
		if !scanner.Scan() || scanner.Text() != fmt.Sprintf(`{"line":%d}`, i) {
			t.Fatal(scanner.Text(), scanner.Err())
		}
	}
	if scanner.Scan() || scanner.Err() != nil {
		t.Fatal(scanner.Err())
	}
}
func BenchmarkEdgeOverhead(b *testing.B) {
	cell := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = io.WriteString(w, `{"ok":true}`) }))
	defer cell.Close()
	e := testutil.New(b, 1, cell.URL, false)
	proxy := httptest.NewServer(&Handler{Config: config.Defaults(), Directory: e.Directory})
	defer proxy.Close()
	client := &http.Client{}
	samples := make([]float64, 0, b.N)
	request := func(url string) time.Duration {
		req, _ := http.NewRequest("GET", url, nil)
		req.Header.Set("X-Gateway-Token", e.Token)
		start := time.Now()
		resp, err := client.Do(req)
		if err != nil {
			b.Fatal(err)
		}
		_, err = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		if err != nil || resp.StatusCode != 200 {
			b.Fatal(err, resp.StatusCode)
		}
		return time.Since(start)
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		direct := request(cell.URL)
		proxied := request(proxy.URL)
		added := float64(proxied-direct) / float64(time.Millisecond)
		if added < 0 {
			added = 0
		}
		samples = append(samples, added)
	}
	b.StopTimer()
	slices.Sort(samples)
	if len(samples) > 0 {
		index := (len(samples)*99+99)/100 - 1
		b.ReportMetric(samples[index], "added-p99-ms")
	}
}
