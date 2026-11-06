package feishu

import (
	"cellrouter/internal/directory"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// LLM: Matches feishu_crypto.compute_signature over exact wire bytes, not parsed JSON.
// 函数用途: 按飞书协议计算签名，不改变事件正文。
func Signature(timestamp, nonce, key string, raw []byte) string {
	h := sha256.New()
	h.Write([]byte(timestamp))
	h.Write([]byte(nonce))
	h.Write([]byte(key))
	h.Write(raw)
	return hex.EncodeToString(h.Sum(nil))
}
func text(m map[string]any, k string) string           { v, _ := m[k].(string); return v }
func object(m map[string]any, k string) map[string]any { v, _ := m[k].(map[string]any); return v }
func one(h http.Header, k string) string {
	var values []string
	for name, v := range h {
		if strings.EqualFold(name, k) {
			values = append(values, v...)
		}
	}
	if len(values) != 1 {
		return ""
	}
	return values[0]
}

// LLM: Signature, freshness, parse and decryption order matches the Cell verifier.
// 函数用途: 先验原始字节再解析和解密，所有异常均拒绝接收。
func Decode(raw []byte, h http.Header, c directory.Credentials, now time.Time, tolerance time.Duration) (map[string]any, error) {
	if c.EncryptKey != "" {
		ts, nonce, sig := one(h, "X-Lark-Request-Timestamp"), one(h, "X-Lark-Request-Nonce"), one(h, "X-Lark-Signature")
		if ts == "" || nonce == "" || subtle.ConstantTimeCompare([]byte(Signature(ts, nonce, c.EncryptKey, raw)), []byte(sig)) != 1 {
			return nil, fmt.Errorf("FEISHU_SIGNATURE")
		}
		n, e := strconv.ParseInt(ts, 10, 64)
		if e != nil {
			return nil, fmt.Errorf("FEISHU_TIMESTAMP")
		}
		delta := now.Sub(time.Unix(n, 0))
		if delta > tolerance || delta < -tolerance {
			return nil, fmt.Errorf("FEISHU_TIMESTAMP")
		}
	}
	var outer map[string]any
	if json.Unmarshal(raw, &outer) != nil || outer == nil {
		return nil, fmt.Errorf("FEISHU_JSON")
	}
	if c.EncryptKey == "" {
		actual := text(outer, "token")
		if actual == "" {
			actual = text(object(outer, "header"), "token")
		}
		if c.VerificationToken == "" || subtle.ConstantTimeCompare([]byte(actual), []byte(c.VerificationToken)) != 1 {
			return nil, fmt.Errorf("FEISHU_TOKEN")
		}
	}
	if _, exists := outer["encrypt"]; !exists {
		return outer, nil
	}
	if c.EncryptKey == "" {
		return nil, fmt.Errorf("FEISHU_ENCRYPT")
	}
	bytes, e := base64.StdEncoding.DecodeString(text(outer, "encrypt"))
	if e != nil || len(bytes) <= aes.BlockSize || (len(bytes)-aes.BlockSize)%aes.BlockSize != 0 {
		return nil, fmt.Errorf("FEISHU_ENCRYPT")
	}
	key := sha256.Sum256([]byte(c.EncryptKey))
	block, e := aes.NewCipher(key[:])
	if e != nil {
		return nil, fmt.Errorf("FEISHU_ENCRYPT")
	}
	plain := make([]byte, len(bytes)-aes.BlockSize)
	cipher.NewCBCDecrypter(block, bytes[:aes.BlockSize]).CryptBlocks(plain, bytes[aes.BlockSize:])
	pad := int(plain[len(plain)-1])
	if pad < 1 || pad > aes.BlockSize || pad > len(plain) {
		return nil, fmt.Errorf("FEISHU_PADDING")
	}
	for _, v := range plain[len(plain)-pad:] {
		if int(v) != pad {
			return nil, fmt.Errorf("FEISHU_PADDING")
		}
	}
	var result map[string]any
	if json.Unmarshal(plain[:len(plain)-pad], &result) != nil || result == nil {
		return nil, fmt.Errorf("FEISHU_JSON")
	}
	return result, nil
}

// LLM: Random nonce is new on every attempt, including redelivery after process restart.
// 函数用途: 清除旧签名后用本次时间和随机 nonce 重新签发转发请求。
func resign(req *http.Request, raw []byte, c directory.Credentials, now time.Time) error {
	for k := range req.Header {
		if signatureHeader(k) {
			delete(req.Header, k)
		}
	}
	if c.EncryptKey == "" {
		return nil
	}
	nonce := make([]byte, 16)
	if _, e := rand.Read(nonce); e != nil {
		return e
	}
	ts := strconv.FormatInt(now.Unix(), 10)
	n := hex.EncodeToString(nonce)
	req.Header.Set("X-Lark-Request-Timestamp", ts)
	req.Header.Set("X-Lark-Request-Nonce", n)
	req.Header.Set("X-Lark-Signature", Signature(ts, n, c.EncryptKey, raw))
	return nil
}
func signatureHeader(k string) bool {
	return strings.EqualFold(k, "X-Lark-Request-Timestamp") || strings.EqualFold(k, "X-Lark-Request-Nonce") || strings.EqualFold(k, "X-Lark-Signature")
}

type ownerIdentity struct{ tenant, kind, id string }

func ownerOf(p map[string]any, card bool) ownerIdentity {
	o := ownerIdentity{tenant: text(object(p, "header"), "tenant_key"), kind: "unknown"}
	ev := object(p, "event")
	if ev == nil {
		ev = p
	}
	if card {
		if id := text(object(ev, "context"), "open_chat_id"); id != "" {
			o.kind = "group"
			o.id = id
			return o
		}
		op := object(ev, "operator")
		o.id = text(op, "open_id")
		if o.id == "" {
			o.id = text(object(op, "operator_id"), "open_id")
		}
		if o.id == "" {
			o.id = text(ev, "open_id")
		}
	} else if msg := object(ev, "message"); msg != nil {
		switch text(msg, "chat_type") {
		case "p2p", "private":
			o.id = text(object(object(ev, "sender"), "sender_id"), "open_id")
		case "group":
			o.kind = "group"
			o.id = text(msg, "chat_id")
			return o
		}
	} else {
		for _, name := range []string{"operator", "user"} {
			v := object(ev, name)
			o.id = text(v, "open_id")
			if o.id == "" {
				o.id = text(object(v, "user_id"), "open_id")
			}
			if o.id != "" {
				break
			}
		}
	}
	if o.id != "" {
		o.kind = "user"
	}
	return o
}
func isCard(p map[string]any) bool {
	ev := object(p, "event")
	if ev == nil {
		ev = p
	}
	v := object(ev, "action")["value"]
	if _, ok := v.(map[string]any); ok {
		return true
	}
	if s, ok := v.(string); ok {
		var m map[string]any
		return json.Unmarshal([]byte(s), &m) == nil && m != nil
	}
	return false
}
func eventKey(p map[string]any, raw []byte) string {
	if id := text(object(p, "header"), "event_id"); id != "" {
		return id
	}
	if id := text(p, "uuid"); id != "" {
		return id
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
