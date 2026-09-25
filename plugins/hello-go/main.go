// LLM: 独立 stdio MCP 服务，只用 Go 标准库；标准输出只写协议帧。工具目录来自编译时嵌入的 declaration.json，
//   与宿主装包时核对的声明同源。本示例不读写工作区，因此不声明工作区读取扩展。改动须同步 declaration.json 与 README.md。
// 模块用途: hello-go 示例插件：编译成单个可执行文件，提供一个问候工具，演示"随包可执行文件"类型的插件。
package main

import (
	"bufio"
	_ "embed"
	"encoding/json"
	"fmt"
	"os"
	"runtime"
)

const maxLine = 131072

//go:embed declaration.json
var declarationJSON []byte

// 类用途: 声明里 MCP 需要的工具字段。
type tool struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"input_schema"`
}

// 类用途: 同源声明中本程序用到的部分。
type declaration struct {
	PluginID string `json:"plugin_id"`
	Version  string `json:"version"`
	Tools    []tool `json:"tools"`
}

// 类用途: 一条 JSON-RPC 请求；ID 为 nil 表示通知，不回复。
type request struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params"`
}

// 类用途: tools/call 的参数。
type callParams struct {
	Name      string          `json:"name"`
	Arguments json.RawMessage `json:"arguments"`
}

// 函数用途: 把结构化结果编码成 MCP 文本内容。
func toolResult(value any, isError bool) map[string]any {
	text, _ := json.Marshal(value)
	return map[string]any{"content": []map[string]any{{"type": "text", "text": string(text)}}, "isError": isError}
}

// 函数用途: 返回问候语，顺带说明编译目标平台与 Go 版本（便于确认宿主运行的就是回执里的那个文件）。
func hello(arguments json.RawMessage) map[string]any {
	var args struct {
		Name string `json:"name"`
	}
	if len(arguments) > 0 && json.Unmarshal(arguments, &args) != nil {
		return toolResult(map[string]string{"code": "INVALID_ARGUMENTS", "message": "参数无效。"}, true)
	}
	name := args.Name
	if name == "" {
		name = "朋友"
	}
	text := fmt.Sprintf("你好，%s！这条回复来自 Go 程序（%s-%s，%s）。", name, runtime.GOOS, runtime.GOARCH, runtime.Version())
	return toolResult(map[string]string{"text": text}, false)
}

// LLM: 业务错误作为 isError 工具结果返回；协议错误用 JSON-RPC error。未初始化前只接受 initialize。
// 函数用途: 处理一条请求，返回响应（通知返回 nil）。
func handle(decl declaration, initialized *bool, req request) map[string]any {
	if req.JSONRPC != "2.0" || req.Method == "" {
		return rpcError(nil, -32600, "请求格式无效。")
	}
	if req.ID == nil {
		return nil
	}
	switch {
	case req.Method == "initialize":
		*initialized = true
		return map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": map[string]any{
			"protocolVersion": "2024-11-05",
			"serverInfo":      map[string]string{"name": decl.PluginID, "version": decl.Version},
			"capabilities":    map[string]any{"tools": map[string]any{}},
		}}
	case !*initialized:
		return rpcError(req.ID, -32000, "请先初始化连接。")
	case req.Method == "ping":
		return map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": map[string]any{}}
	case req.Method == "tools/list":
		tools := make([]map[string]any, 0, len(decl.Tools))
		for _, item := range decl.Tools {
			tools = append(tools, map[string]any{"name": item.Name, "description": item.Description, "inputSchema": item.InputSchema})
		}
		return map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": map[string]any{"tools": tools}}
	case req.Method == "tools/call":
		var params callParams
		if json.Unmarshal(req.Params, &params) != nil || params.Name != "hello" {
			return map[string]any{"jsonrpc": "2.0", "id": req.ID,
				"result": toolResult(map[string]string{"code": "UNKNOWN_TOOL", "message": "工具不存在。"}, true)}
		}
		return map[string]any{"jsonrpc": "2.0", "id": req.ID, "result": hello(params.Arguments)}
	}
	return rpcError(req.ID, -32601, "不支持此方法。")
}

// 函数用途: 生成 JSON-RPC 错误响应。
func rpcError(id json.RawMessage, code int, message string) map[string]any {
	var value any = id
	if id == nil {
		value = nil
	}
	return map[string]any{"jsonrpc": "2.0", "id": value, "error": map[string]any{"code": code, "message": message}}
}

// LLM: 单行有上限，超长或读错直接退出，交由宿主记录真实退出；stdin 关闭即结束。
// 函数用途: 逐行读取标准输入并写回响应。
func main() {
	var decl declaration
	if err := json.Unmarshal(declarationJSON, &decl); err != nil {
		fmt.Fprintln(os.Stderr, "插件内置声明无效。")
		os.Exit(2)
	}
	initialized := false
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 64*1024), maxLine)
	writer := bufio.NewWriter(os.Stdout)
	encoder := json.NewEncoder(writer)
	encoder.SetEscapeHTML(false)
	for scanner.Scan() {
		var req request
		var response map[string]any
		if err := json.Unmarshal(scanner.Bytes(), &req); err != nil {
			response = rpcError(nil, -32700, "请求不是有效 JSON。")
		} else {
			response = handle(decl, &initialized, req)
		}
		if response != nil {
			_ = encoder.Encode(response)
			_ = writer.Flush()
		}
	}
	if scanner.Err() != nil {
		fmt.Fprintln(os.Stderr, "插件请求超过读取上限或读取失败。")
		os.Exit(2)
	}
}
