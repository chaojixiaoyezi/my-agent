// Package testutil constructs isolated seeds and generated credentials for local tests.
package testutil

import (
	"cellrouter/internal/directory"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

type Env struct {
	Seed        directory.Seed
	Path        string
	Directory   *directory.Store
	Token       string
	CellToken   string
	Credentials directory.Credentials
}

func Random(t testing.TB) string {
	t.Helper()
	b := make([]byte, 32)
	if _, e := rand.Read(b); e != nil {
		t.Fatal(e)
	}
	return hex.EncodeToString(b)
}
func Write(t testing.TB, path string, v any) {
	t.Helper()
	b, e := json.Marshal(v)
	if e != nil {
		t.Fatal(e)
	}
	if e = os.WriteFile(path, b, 0600); e != nil {
		t.Fatal(e)
	}
}
func (e *Env) Save(t testing.TB) { Write(t, e.Path, e.Seed) }

// LLM: All secret bytes are generated in a temporary directory, never committed fixtures.
// 函数用途: 为测试生成具有真实挂载文件形状的单应用目录。
func New(t testing.TB, count int, upstream string, encrypted bool) *Env {
	t.Helper()
	root := t.TempDir()
	token := Random(t)
	cellToken := Random(t)
	cred := directory.Credentials{VerificationToken: Random(t)}
	if encrypted {
		cred.EncryptKey = Random(t)
	}
	credDir := filepath.Join(root, "credentials")
	if e := os.Mkdir(credDir, 0700); e != nil {
		t.Fatal(e)
	}
	for name, v := range map[string]string{"encrypt_key": cred.EncryptKey, "verification_token": cred.VerificationToken} {
		if e := os.WriteFile(filepath.Join(credDir, name), []byte(v), 0600); e != nil {
			t.Fatal(e)
		}
	}
	tokenPath := filepath.Join(root, "cell-token")
	if e := os.WriteFile(tokenPath, []byte(cellToken), 0600); e != nil {
		t.Fatal(e)
	}
	s := directory.Seed{Version: 1, Apps: []directory.App{{AppID: "app-example", Routing: "by_owner", CredentialRef: credDir}}}
	for i := 0; i < count; i++ {
		id := fmt.Sprintf("cell-%02d", i)
		org := fmt.Sprintf("org-%02d", i)
		owner := fmt.Sprintf("ou_example_%02d", i)
		s.Cells = append(s.Cells, directory.Cell{CellID: id, BaseURL: upstream, FeishuCallbackURL: upstream + "/feishu/callback", TokenSecretRef: tokenPath, FeishuAppID: "app-example", CapacityUsers: 1000, State: "serving"})
		s.Orgs = append(s.Orgs, directory.Org{OrgID: org, CellID: id, State: "active"})
		s.Owners = append(s.Owners, directory.Owner{Provider: "feishu", AppID: "app-example", TenantKey: "tenant-example", OwnerKind: "user", ProviderOwnerID: owner, LocalOwnerID: owner, OrgID: org})
	}
	hash := sha256.Sum256([]byte(token))
	s.Principals = []directory.Principal{{TokenSHA256: hex.EncodeToString(hash[:]), PrincipalKind: "feishu", LocalOwnerID: s.Owners[0].LocalOwnerID, OrgID: s.Orgs[0].OrgID, State: "active"}}
	e := &Env{Seed: s, Path: filepath.Join(root, "directory.json"), Token: token, CellToken: cellToken, Credentials: cred}
	e.Save(t)
	var err error
	e.Directory, err = directory.NewStore(e.Path)
	if err != nil {
		t.Fatal(err)
	}
	return e
}
