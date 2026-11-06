// cell-router runs public webhook/edge and private administration listeners.
package main

import (
	"cellrouter/internal/config"
	"cellrouter/internal/directory"
	"cellrouter/internal/edge"
	"cellrouter/internal/feishu"
	"cellrouter/internal/store"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"sync/atomic"
	"syscall"
)

// LLM: No secret values are accepted on argv or included in config diagnostics.
// 函数用途: 加载文件配置并以前台进程运行双监听路由器。
func main() {
	path := flag.String("config", "", "router config JSON path")
	flag.Parse()
	c, e := config.Load(*path)
	if e != nil {
		log.Fatalf("configuration: %s", e)
	}
	if e = run(c); e != nil {
		log.Fatal(e)
	}
}

// LLM: One process owns the SQLite PVC; listeners drain before workers are cancelled.
// 函数用途: 装配目录、账本和请求入口，处理热重载及有界退出。
func run(c config.Config) error {
	d, e := directory.NewStore(c.DirectorySeedPath)
	if e != nil {
		return e
	}
	s, e := store.Open(c.ReceiptsDBPath)
	if e != nil {
		return fmt.Errorf("STORE_OPEN")
	}
	defer s.Close()
	if e = s.ResetDelivering(); e != nil {
		return fmt.Errorf("STORE_RESET")
	}
	if e = s.PurgeOlderThan(c.ReceiptRetention.Duration); e != nil {
		return fmt.Errorf("STORE_PURGE")
	}
	transport := &http.Transport{Proxy: http.ProxyFromEnvironment, MaxIdleConns: c.HTTPMaxIdleConnections, MaxIdleConnsPerHost: c.ForwardWorkers, IdleConnTimeout: c.HTTPIdleTimeout.Duration}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	f := feishu.NewForwarder(c, d, s, client)
	ingress := feishu.NewHandler(c, d, s, client, f.Notify)
	edgeHandler := &edge.Handler{Config: c, Directory: d, Transport: transport}
	pub := http.NewServeMux()
	pub.Handle("/feishu/", ingress)
	pub.Handle("/", edgeHandler)
	for _, path := range []string{"/healthz", "/readyz", "/configz", "/receipts", "/dlq", "/dlq/replay", "/authz"} {
		pub.HandleFunc(path, http.NotFound)
	}
	admin := http.NewServeMux()
	admin.Handle("/authz", edgeHandler)
	f.RegisterAdmin(admin)
	var ready atomic.Bool
	admin.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusOK) })
	admin.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		_, _, e := s.NextDue()
		if !ready.Load() || e != nil {
			http.Error(w, "NOT_READY", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
	})
	admin.HandleFunc("GET /configz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(c)
	})
	publicServer := &http.Server{Addr: c.ListenAddr, Handler: pub, ReadHeaderTimeout: c.ReadHeaderTimeout.Duration, MaxHeaderBytes: c.MaxHeaderBytes}
	adminServer := &http.Server{Addr: c.AdminAddr, Handler: admin, ReadHeaderTimeout: c.ReadHeaderTimeout.Duration, MaxHeaderBytes: c.MaxHeaderBytes}
	publicListener, e := net.Listen("tcp", c.ListenAddr)
	if e != nil {
		return e
	}
	defer publicListener.Close()
	adminListener, e := net.Listen("tcp", c.AdminAddr)
	if e != nil {
		return e
	}
	defer adminListener.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { f.Run(ctx); close(done) }()
	fail := make(chan error, 2)
	go func() { fail <- publicServer.Serve(publicListener) }()
	go func() { fail <- adminServer.Serve(adminListener) }()
	b, _ := json.Marshal(c)
	log.Printf("effective_config=%s", b)
	ready.Store(true)
	signals := make(chan os.Signal, 1)
	reloads := make(chan os.Signal, 1)
	// A pending reload must never occupy the termination notification slot.
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	signal.Notify(reloads, syscall.SIGHUP)
	defer signal.Stop(signals)
	defer signal.Stop(reloads)
	running := true
	var serveErr error
	for running {
		select {
		case <-signals:
			running = false
		case <-reloads:
			if d.Reload() == nil {
				f.Notify()
			}
		case serveErr = <-fail:
			running = false
		}
	}
	ready.Store(false)
	drain, stop := context.WithTimeout(context.Background(), c.ShutdownTimeout.Duration)
	defer stop()
	if publicServer.Shutdown(drain) != nil {
		_ = publicServer.Close()
	}
	if adminServer.Shutdown(drain) != nil {
		_ = adminServer.Close()
	}
	cancel()
	select {
	case <-done:
	case <-drain.Done():
		return fmt.Errorf("SHUTDOWN_TIMEOUT")
	}
	if serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
		return serveErr
	}
	return nil
}
