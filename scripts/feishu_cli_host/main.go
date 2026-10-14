// LLM: 宿主通过私有 stdin 固定一次已授权请求；官方 CLI 负责执行，扩展只约束运输与返回原始结果。
// 模块用途: 将 lark-cli 嵌入无登录缓存的单请求后端，不提供任意命令或秘密导出入口。
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"

	"github.com/larksuite/cli/cmd"
	"github.com/larksuite/cli/extension/credential"
	"github.com/larksuite/cli/extension/platform"
	"github.com/larksuite/cli/extension/transport"
)

const protocol = "my-agent.feishu-cli.v1"
const maxBytes = 4 * 1024 * 1024

// LLM: 身份来自宿主冻结的上下文，参数不可选地址、文件或命令；认证只在当前进程内保留。
// 类用途: 接收一次官方云请求的私有执行载荷。
type hostRequest struct {
	Schema   string          `json:"schema"`
	Method   string          `json:"method"`
	Path     string          `json:"path"`
	Body     json.RawMessage `json:"body"`
	AppID    string          `json:"app_id"`
	Token    string          `json:"token"`
	Identity string          `json:"identity"`
	ActorID  string          `json:"actor_id,omitempty"`
}

// LLM: 返回 HTTP 和供应商原始 JSON，不把 CLI 的 ok 包装当供应商成功；异常文本不进入协议。
// 类用途: 让 Python 原错误分类与未知写入账判断本次结果。
type hostResult struct {
	Schema      string          `json:"schema"`
	HTTPStatus  int             `json:"http_status"`
	Response    json.RawMessage `json:"response,omitempty"`
	RequestSent bool            `json:"request_sent"`
	ErrorCode   string          `json:"error_code,omitempty"`
}

// LLM: 提供本次固定应用/用户 token；不查询配置、keychain 或其他帐号。
// 类用途: 给官方 credential 扩展提供单次身份。
type hostProvider struct {
	request           hostRequest
	result            hostResult
	target            *url.URL
	attempted         bool
	identityAttempted bool
	identityVerified  bool
}

// LLM: 扩展名称仅用于源码身份，不输出私人载荷。
// 函数用途: 登记当前宿主扩展。
func (h *hostProvider) Name() string { return "my-agent-host" }

// LLM: 优先于任何默认凭据来源；不得改成用户缓存回退。
// 函数用途: 使本次宿主认证成为唯一来源。
func (h *hostProvider) Priority() int { return -100 }

// LLM: 仅固定当前 token 类型和应用；不提供 app_secret。
// 函数用途: 给 CLI 定位当前应用和允许身份。
func (h *hostProvider) ResolveAccount(context.Context) (*credential.Account, error) {
	support := credential.SupportsBot
	identity := credential.IdentityBot
	if h.request.Identity == "user" {
		support = credential.SupportsUser
		identity = credential.IdentityUser
	}
	return &credential.Account{AppID: h.request.AppID, Brand: credential.BrandFeishu, DefaultAs: identity, SupportedIdentities: support}, nil
}

// LLM: token 请求必须与当前冻结身份一致，冲突主动阻断其他 provider。
// 函数用途: 为一次云调用注入应用或本人 token。
func (h *hostProvider) ResolveToken(_ context.Context, spec credential.TokenSpec) (*credential.Token, error) {
	wanted := credential.TokenTypeTAT
	if h.request.Identity == "user" {
		wanted = credential.TokenTypeUAT
	}
	if spec.AppID != h.request.AppID || spec.Type != wanted {
		return nil, &credential.BlockError{Provider: h.Name(), Reason: "identity_mismatch"}
	}
	return &credential.Token{Value: h.request.Token, Source: "host-private-input"}, nil
}

// LLM: 原 API 请求与重定向均经过该运输门；检查每次准确目标，不授予任何新资源权限。
// 函数用途: 固定进程内唯一运输拦截器。
func (h *hostProvider) ResolveInterceptor(context.Context) transport.Interceptor { return h }

// LLM: 官方 AbortableInterceptor 使用 E 版本；普通版本保持接口完整。
// 函数用途: 提供官方运输扩展的兼容方法。
func (h *hostProvider) PreRoundTrip(*http.Request) func(*http.Response, error) { return nil }

// LLM: 本人仅先验一次固定userinfo，严格核对宿主actor；业务仅一次，跳转/重试和未验身份均拒绝。
// 函数用途: 保持官方身份生命周期并阻止未知写入重复，认证资料不进入业务结果。
func (h *hostProvider) PreRoundTripE(r *http.Request) (func(*http.Response, error), error) {
	if h.request.Identity == "user" && !h.identityAttempted && !h.attempted && r.Method == "GET" && r.URL.String() == "https://open.feishu.cn/open-apis/authen/v1/user_info" && r.Body == nil && r.Header.Get("Authorization") == "Bearer "+h.request.Token {
		h.identityAttempted = true
		return h.captureIdentity, nil
	}
	if h.request.Identity == "user" && !h.identityVerified {
		h.result.ErrorCode = "identity_unverified"
		return nil, errors.New("identity_unverified")
	}
	if h.attempted {
		h.result.ErrorCode = "replay_refused"
		return nil, errors.New("request_replay_refused")
	}
	if r.Method != h.request.Method || r.URL.Scheme != "https" || r.URL.Host != "open.feishu.cn" || r.URL.User != nil || r.URL.Fragment != "" || r.URL.EscapedPath() != h.target.EscapedPath() || r.URL.Query().Encode() != h.target.Query().Encode() {
		h.result.ErrorCode = "request_scope_mismatch"
		return nil, errors.New("request_scope_mismatch")
	}
	h.attempted = true
	h.result.RequestSent = true
	return h.captureResponse, nil
}

// LLM: 本人验证与业务副作用分开；只认宿主冻结actor和严格code，官方非致命错误不能绕过业务门。
// 函数用途: 验证官方预查询的当前UAT账号；失败时后续业务不发送。
func (h *hostProvider) captureIdentity(r *http.Response, err error) {
	if err != nil || r == nil {
		return
	}
	raw, readErr := io.ReadAll(io.LimitReader(r.Body, maxBytes+1))
	_ = r.Body.Close()
	r.Body = io.NopCloser(bytes.NewReader(raw))
	if readErr != nil || len(raw) > maxBytes || r.StatusCode != 200 {
		return
	}
	var value struct {
		Code json.RawMessage `json:"code"`
		Data struct {
			OpenID string `json:"open_id"`
		} `json:"data"`
	}
	if json.Unmarshal(raw, &value) != nil || string(value.Code) != "0" || h.request.ActorID == "" || value.Data.OpenID != h.request.ActorID {
		return
	}
	h.identityVerified = true
}

// LLM: 不把供应商正文放进异常；恢复 body 给官方 CLI，保留准确 status/code供宿主判断。
// 函数用途: 捕获唯一 HTTP 响应并拒绝超大、损坏和重定向结果。
func (h *hostProvider) captureResponse(r *http.Response, err error) {
	if err != nil || r == nil {
		h.result.ErrorCode = "transport_unknown"
		return
	}
	h.result.HTTPStatus = r.StatusCode
	if r.StatusCode >= 300 && r.StatusCode < 400 {
		h.result.ErrorCode = "redirect_refused"
	}
	raw, readErr := io.ReadAll(io.LimitReader(r.Body, maxBytes+1))
	_ = r.Body.Close()
	r.Body = io.NopCloser(bytes.NewReader(raw))
	if readErr != nil || len(raw) > maxBytes || !json.Valid(raw) {
		h.result.ErrorCode = "response_invalid"
		return
	}
	var envelope map[string]json.RawMessage
	if json.Unmarshal(raw, &envelope) != nil {
		h.result.ErrorCode = "response_invalid"
		return
	}
	code := string(envelope["code"])
	if !integerCode(code) {
		h.result.ErrorCode = "provider_code_invalid"
		return
	}
	h.result.Response = raw
}

// LLM: 与 Python type(code) is int 合同一致，拒绝浮点、字符串和缺字段；不解析供应商话术。
// 函数用途: 验证供应商 code 的整数 JSON 类型。
func integerCode(value string) bool {
	if strings.HasPrefix(value, "-") {
		value = value[1:]
	}
	if value == "" {
		return false
	}
	for _, r := range value {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// LLM: stdio 是私有运输；请求只来自宿主，官方参数由结构字段生成，模型不能提供命令字符串。
// 函数用途: 读取一次请求，执行受限官方 CLI 并输出一次结构化结果。
func run() hostResult {
	result := hostResult{Schema: protocol}
	raw, err := io.ReadAll(io.LimitReader(os.Stdin, maxBytes+1))
	var request hostRequest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err != nil || len(raw) > maxBytes || decoder.Decode(&request) != nil || decoder.Decode(new(any)) != io.EOF || request.Schema != protocol || request.AppID == "" || request.Token == "" || (request.Identity != "bot" && request.Identity != "user") || (request.Identity == "user" && request.ActorID == "") || !strings.HasPrefix(request.Path, "/") || strings.HasPrefix(request.Path, "//") {
		result.ErrorCode = "request_invalid"
		return result
	}
	target, err := url.Parse("https://open.feishu.cn/open-apis" + request.Path)
	if err != nil || target.Fragment != "" {
		result.ErrorCode = "request_invalid"
		return result
	}
	for _, segment := range strings.Split(target.Path, "/") {
		if segment == ".." {
			result.ErrorCode = "request_invalid"
			return result
		}
	}
	if request.Method != "GET" && request.Method != "POST" && request.Method != "PUT" && request.Method != "PATCH" && request.Method != "DELETE" {
		result.ErrorCode = "request_invalid"
		return result
	}
	h := &hostProvider{request: request, result: result, target: target}
	credential.Register(h)
	transport.Register(h)
	platform.Register(platform.NewPlugin("my-agent-host", "1").Restrict(&platform.Rule{Allow: []string{"api"}, AllowUnannotated: true}).FailClosed().MustBuild())
	args := []string{"my-agent-feishu-cli", "api", request.Method, target.EscapedPath(), "--as", request.Identity}
	if len(target.Query()) > 0 {
		query := map[string]string{}
		for k, v := range target.Query() {
			if len(v) != 1 {
				h.result.ErrorCode = "request_invalid"
				return h.result
			}
			query[k] = v[0]
		}
		body, _ := json.Marshal(query)
		args = append(args, "--params", string(body))
	}
	input := strings.NewReader("")
	if len(request.Body) > 0 && string(request.Body) != "null" {
		args = append(args, "--data", "-")
		input = strings.NewReader(string(request.Body))
	}
	os.Args = args
	exit := cmd.ExecuteWithOptions(cmd.WithIO(input, io.Discard, io.Discard), cmd.ConcealRestrictedCommands())
	if exit != 0 && h.result.ErrorCode == "" && len(h.result.Response) == 0 {
		h.result.ErrorCode = "cli_execution_failed"
	}
	return h.result
}

// LLM: 进程不输出 CLI 文案或秘密，只写固定协议结果。
// 函数用途: 提供随包宿主 CLI 入口。
func main() { _ = json.NewEncoder(os.Stdout).Encode(run()) }
