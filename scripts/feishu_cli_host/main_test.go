package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"testing"
)

type syntheticTransport struct {
	calls, businessCalls int
	mode                 string
}

func (s *syntheticTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	s.calls++
	if r.Header.Get("Authorization") != "Bearer synthetic-host-token" {
		return nil, io.ErrUnexpectedEOF
	}
	if r.URL.String() == "https://open.feishu.cn/open-apis/authen/v1/user_info" && r.Method == "GET" {
		body := `{"code":0,"data":{"open_id":"ou_synthetic","name":"synthetic"}}`
		status := 200
		if s.mode == "wrong-user" {
			body = `{"code":0,"data":{"open_id":"ou_other"}}`
		}
		if s.mode == "malformed-user" {
			body = `{"code":"0","data":{"open_id":"ou_synthetic"}}`
		}
		if s.mode == "redirect-user" {
			status = 307
		}
		headers := http.Header{"Content-Type": {"application/json"}}
		if status == 307 {
			headers.Set("Location", "https://evil.invalid/credential")
		}
		return &http.Response{StatusCode: status, Header: headers, Body: io.NopCloser(strings.NewReader(body))}, nil
	}
	if r.URL.String() != "https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/synthetic/values_append" || r.Method != "POST" {
		return nil, io.ErrUnexpectedEOF
	}
	s.businessCalls++
	raw, err := io.ReadAll(r.Body)
	if err != nil || !strings.Contains(string(raw), "sid!A1") || !strings.Contains(string(raw), "synthetic") {
		return nil, io.ErrUnexpectedEOF
	}
	return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {"application/json"}}, Body: io.NopCloser(strings.NewReader(`{"code":0,"data":{"valueRange":{"values":[["synthetic"]]}}}`))}, nil
}

func TestOfficialCommandWithHostedIdentity(t *testing.T) {
	mode := os.Getenv("MY_AGENT_TEST_CLI_MODE")
	if mode == "" {
		for _, value := range []string{"bot", "user", "wrong-user", "malformed-user", "redirect-user"} {
			t.Run(value, func(t *testing.T) {
				command := exec.Command(os.Args[0], "-test.run=^TestOfficialCommandWithHostedIdentity$")
				command.Env = append(os.Environ(), "MY_AGENT_TEST_CLI_MODE="+value)
				if output, err := command.CombinedOutput(); err != nil {
					t.Fatalf("isolated official command: %s %v", output, err)
				}
			})
		}
		return
	}
	t.Setenv("HOME", t.TempDir())
	t.Setenv("LARKSUITE_CLI_CONFIG_DIR", t.TempDir())
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	identity := "user"
	if mode == "bot" {
		identity = "bot"
	}
	request := hostRequest{Schema: protocol, Method: "POST", Path: "/sheets/v2/spreadsheets/synthetic/values_append", AppID: "cli_synthetic", Token: "synthetic-host-token", Identity: identity, ActorID: "ou_synthetic", Body: json.RawMessage(`{"valueRange":{"range":"sid!A1","values":[["synthetic"]]}}`)}
	if err := json.NewEncoder(w).Encode(request); err != nil {
		t.Fatal(err)
	}
	w.Close()
	oldStdin, oldArgs, oldTransport := os.Stdin, os.Args, http.DefaultTransport
	defer func() { os.Stdin, os.Args, http.DefaultTransport = oldStdin, oldArgs, oldTransport }()
	target := &syntheticTransport{mode: mode}
	os.Stdin, http.DefaultTransport = r, target
	result := run()
	if mode != "bot" && mode != "user" {
		if result.RequestSent || target.businessCalls != 0 || result.ErrorCode != "identity_unverified" {
			t.Fatalf("unverified user business call: %#v calls=%d", result, target.businessCalls)
		}
		return
	}
	calls := 1
	if mode == "user" {
		calls = 2
	}
	if result.ErrorCode != "" || result.HTTPStatus != 200 || !result.RequestSent || len(result.Response) == 0 || target.calls != calls || target.businessCalls != 1 {
		t.Fatalf("official hosted call failed: %#v calls=%d", result, target.calls)
	}
}

func newProvider(t *testing.T) *hostProvider {
	t.Helper()
	u, _ := url.Parse("https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/synthetic/values_append")
	return &hostProvider{request: hostRequest{Method: "POST"}, target: u, result: hostResult{Schema: protocol}}
}

func TestScopeAndNoReplay(t *testing.T) {
	h := newProvider(t)
	r, _ := http.NewRequest("POST", h.target.String(), nil)
	post, err := h.PreRoundTripE(r)
	if err != nil || post == nil {
		t.Fatal("authorized request rejected")
	}
	if _, err = h.PreRoundTripE(r); err == nil {
		t.Fatal("request replay allowed")
	}
	for _, target := range []string{"https://evil.invalid/open-apis/sheets/v2/spreadsheets/synthetic/values_append", "https://open.feishu.cn/open-apis/other", "http://open.feishu.cn/open-apis/sheets/v2/spreadsheets/synthetic/values_append"} {
		x := newProvider(t)
		r, _ = http.NewRequest("POST", target, nil)
		if _, err = x.PreRoundTripE(r); err == nil || x.result.RequestSent {
			t.Fatal("scope mismatch sent")
		}
	}
}

func TestRawProviderCode(t *testing.T) {
	for _, body := range []string{`{}`, `{"code":"0"}`, `{"code":0.0}`, `{"code":true}`, `[]`, `not json`} {
		h := newProvider(t)
		h.captureResponse(&http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil)
		if h.result.ErrorCode == "" || len(h.result.Response) > 0 {
			t.Fatal("invalid provider response accepted")
		}
	}
	for _, body := range []string{`{"code":0,"data":{"value":1}}`, `{"code":91403}`} {
		h := newProvider(t)
		h.captureResponse(&http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil)
		if h.result.ErrorCode != "" || string(h.result.Response) != body {
			t.Fatal("raw result lost")
		}
	}
}

func TestRedirectRefused(t *testing.T) {
	h := newProvider(t)
	h.captureResponse(&http.Response{StatusCode: 307, Body: io.NopCloser(strings.NewReader(`{"code":0}`))}, nil)
	if h.result.ErrorCode != "redirect_refused" || h.result.HTTPStatus != 307 {
		t.Fatal("redirect accepted")
	}
}
