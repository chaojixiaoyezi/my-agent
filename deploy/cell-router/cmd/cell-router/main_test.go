package main

import (
	"bytes"
	"cellrouter/internal/config"
	"cellrouter/internal/testutil"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

func TestRouterHelperProcess(t *testing.T) {
	path := os.Getenv("CELL_ROUTER_TEST_CONFIG")
	if path == "" {
		return
	}
	c, e := config.Load(path)
	if e == nil {
		e = run(c)
	}
	if e != nil {
		t.Fatal(e)
	}
}
func freeAddress(t *testing.T) string {
	t.Helper()
	l, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	a := l.Addr().String()
	l.Close()
	return a
}
func eventually(t *testing.T, check func() bool) {
	t.Helper()
	end := time.Now().Add(5 * time.Second)
	for time.Now().Before(end) {
		if check() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("observable router state did not converge")
}

// LLM: Exercise the actual composition root in a fresh process, not an isolated handler.
// 函数用途: 经真实双监听验证启动、投递、目录重载、管理隔离和退出。
func TestRouterProcessLifecycle(t *testing.T) {
	var calls atomic.Int32
	businessHeaders := make(chan http.Header, 1)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/feishu/callback" {
			calls.Add(1)
		}
		if r.URL.Path == "/client/business/send" {
			businessHeaders <- r.Header.Clone()
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"ok":true}`)
	}))
	defer upstream.Close()
	env := testutil.New(t, 1, upstream.URL, false)
	root := t.TempDir()
	c := config.Defaults()
	c.ListenAddr = freeAddress(t)
	c.AdminAddr = freeAddress(t)
	c.DirectorySeedPath = env.Path
	c.ReceiptsDBPath = filepath.Join(root, "receipts.sqlite")
	c.ShutdownTimeout = config.Duration{Duration: 2 * time.Second}
	path := filepath.Join(root, "config.json")
	testutil.Write(t, path, c)
	logfile, e := os.Create(filepath.Join(root, "process.log"))
	if e != nil {
		t.Fatal(e)
	}
	defer logfile.Close()
	cmd := exec.Command(os.Args[0], "-test.run=^TestRouterHelperProcess$")
	cmd.Env = append(os.Environ(), "CELL_ROUTER_TEST_CONFIG="+path)
	cmd.Stdout = logfile
	cmd.Stderr = logfile
	if e = cmd.Start(); e != nil {
		t.Fatal(e)
	}
	ended := make(chan error, 1)
	go func() { ended <- cmd.Wait() }()
	stopped := false
	defer func() {
		if !stopped {
			_ = cmd.Process.Kill()
			<-ended
		}
		if t.Failed() {
			b, _ := os.ReadFile(logfile.Name())
			t.Log(string(b))
		}
	}()
	client := &http.Client{Timeout: time.Second}
	admin := "http://" + c.AdminAddr
	public := "http://" + c.ListenAddr
	get := func(url string, personal bool) (int, []byte, http.Header) {
		req, _ := http.NewRequest("GET", url, nil)
		if personal {
			req.Header.Set("X-Gateway-Token", env.Token)
		}
		resp, e := client.Do(req)
		if e != nil {
			return 0, nil, nil
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, b, resp.Header
	}
	eventually(t, func() bool { status, _, _ := get(admin+"/readyz", false); return status == 200 })
	status, b, _ := get(admin+"/configz", false)
	var effective config.Config
	if e = json.Unmarshal(b, &effective); e != nil || status != 200 || effective != c {
		t.Fatal(status, string(b), e)
	}
	for _, p := range []string{"/readyz", "/healthz", "/configz", "/receipts", "/dlq", "/dlq/replay", "/authz"} {
		status, body, headers := get(public+p, true)
		if status != 404 || headers.Get("X-Gateway-Token") != "" || bytes.Contains(body, []byte(env.CellToken)) {
			t.Fatal("public admin route not isolated", p, status)
		}
	}
	status, _, _ = get(public+"/authz", false)
	if status != 404 {
		t.Fatal("public authz without personal credential", status)
	}
	status, _, headers := get(admin+"/authz", true)
	if status != 200 || headers.Get("X-User-Id") != "ou_example_00" || headers.Get("X-Channel") != "feishu" || headers.Get("X-Gateway-Token") != env.CellToken || headers.Get("X-Cell-Upstream") != upstream.URL {
		t.Fatal(status, headers)
	}
	status, _, headers = get(admin+"/authz", false)
	if status != 401 || headers.Get("X-Gateway-Token") != "" {
		t.Fatal("protected authz accepted missing credential", status)
	}
	status, _, headers = get(public+"/client/business/send", true)
	if status != 200 || headers.Get("X-Gateway-Token") != "" {
		t.Fatal("ordinary public proxy changed", status)
	}
	select {
	case headers = <-businessHeaders:
		if headers.Get("X-Gateway-Token") != env.CellToken || headers.Get("X-User-Id") != "ou_example_00" || headers.Get("X-Channel") != "feishu" {
			t.Fatal("ordinary public proxy identity changed")
		}
	default:
		t.Fatal("ordinary public request did not reach Cell")
	}
	raw, _ := json.Marshal(map[string]any{"header": map[string]string{"event_id": "event-example", "tenant_key": "tenant-example", "token": env.Credentials.VerificationToken}, "event": map[string]any{"sender": map[string]any{"sender_id": map[string]string{"open_id": "ou_example_00"}}, "message": map[string]string{"chat_type": "p2p"}}})
	for i := 0; i < 2; i++ {
		resp, e := client.Post(public+"/feishu/app-example/callback", "application/json", bytes.NewReader(raw))
		if e != nil {
			t.Fatal(e)
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		if resp.StatusCode != 200 {
			t.Fatal(resp.StatusCode)
		}
	}
	eventually(t, func() bool {
		status, b, _ := get(admin+"/receipts?app_id=app-example&event_id=event-example", false)
		var r struct{ State string }
		_ = json.Unmarshal(b, &r)
		return status == 200 && r.State == "delivered"
	})
	if calls.Load() != 1 {
		t.Fatal("duplicate forwarded", calls.Load())
	}
	env.Seed.Cells[0].State = "frozen"
	env.Seed.Version++
	env.Save(t)
	if e = cmd.Process.Signal(syscall.SIGHUP); e != nil {
		t.Fatal(e)
	}
	eventually(t, func() bool { status, _, _ := get(admin+"/authz", true); return status == 503 })
	env.Seed.Cells[0].State = "serving"
	env.Seed.Owners[0].LocalOwnerID = "admin"
	env.Save(t)
	if e = cmd.Process.Signal(syscall.SIGHUP); e != nil {
		t.Fatal(e)
	}
	eventually(t, func() bool {
		b, _ := os.ReadFile(logfile.Name())
		return strings.Contains(string(b), "DIRECTORY_OWNER")
	})
	status, _, _ = get(admin+"/authz", true)
	if status != 503 {
		t.Fatal("invalid reload replaced previous snapshot", status)
	}
	env.Seed.Owners[0].LocalOwnerID = env.Seed.Owners[0].ProviderOwnerID
	env.Seed.Cells[0].State = "serving"
	env.Seed.Version++
	env.Save(t)
	if e = cmd.Process.Signal(syscall.SIGHUP); e != nil {
		t.Fatal(e)
	}
	eventually(t, func() bool { status, _, _ := get(admin+"/authz", true); return status == 200 })
	// Reload traffic must not consume the termination notification slot.
	if e = cmd.Process.Signal(syscall.SIGHUP); e != nil {
		t.Fatal(e)
	}
	if e = cmd.Process.Signal(syscall.SIGTERM); e != nil {
		t.Fatal(e)
	}
	select {
	case e = <-ended:
		stopped = true
		if e != nil {
			t.Fatal(e)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("graceful exit timed out")
	}
}
