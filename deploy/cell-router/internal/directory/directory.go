// Package directory loads a validated, immutable organisation-closure seed.
package directory

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"unicode"
)

type Cell struct {
	CellID            string `json:"cell_id"`
	BaseURL           string `json:"base_url"`
	FeishuCallbackURL string `json:"feishu_callback_url"`
	TokenSecretRef    string `json:"token_secret_ref"`
	FeishuAppID       string `json:"feishu_app_id"`
	CapacityUsers     int    `json:"capacity_users"`
	State             string `json:"state"`
}
type Org struct {
	OrgID  string `json:"org_id"`
	CellID string `json:"cell_id"`
	State  string `json:"state"`
}
type Owner struct {
	Provider        string `json:"provider"`
	AppID           string `json:"app_id"`
	TenantKey       string `json:"tenant_key"`
	OwnerKind       string `json:"owner_kind"`
	ProviderOwnerID string `json:"provider_owner_id"`
	LocalOwnerID    string `json:"local_owner_id"`
	OrgID           string `json:"org_id"`
}
type Principal struct {
	TokenSHA256   string `json:"token_sha256"`
	PrincipalKind string `json:"principal_kind"`
	LocalOwnerID  string `json:"local_owner_id"`
	OrgID         string `json:"org_id"`
	State         string `json:"state"`
}
type App struct {
	AppID         string  `json:"app_id"`
	Routing       string  `json:"routing"`
	CellID        *string `json:"cell_id"`
	CredentialRef string  `json:"credential_ref"`
}
type Credentials struct {
	EncryptKey        string
	VerificationToken string
}
type Seed struct {
	Version    int64       `json:"version"`
	Cells      []Cell      `json:"cells"`
	Orgs       []Org       `json:"orgs"`
	Owners     []Owner     `json:"owners"`
	Principals []Principal `json:"principals"`
	Apps       []App       `json:"feishu_apps"`
}
type ownerKey struct{ provider, app, tenant, kind, id string }
type Directory struct {
	Version     int64
	cells       map[string]Cell
	orgs        map[string]Org
	owners      map[ownerKey]Owner
	principals  map[string]Principal
	apps        map[string]App
	credentials map[string]Credentials
	tokens      map[string]string
}

var hashPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
var webPattern = regexp.MustCompile(`^web:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

func validID(v string) bool {
	return v != "" && !strings.EqualFold(v, "admin") && strings.IndexFunc(v, unicode.IsSpace) < 0 && !strings.ContainsAny(v, "/\\\x00\r\n")
}
func validURL(v string) bool {
	u, e := url.Parse(v)
	return e == nil && (u.Scheme == "http" || u.Scheme == "https") && u.Host != "" && u.User == nil && u.RawQuery == "" && u.Fragment == ""
}
func key(o Owner) ownerKey {
	return ownerKey{o.Provider, o.AppID, o.TenantKey, o.OwnerKind, o.ProviderOwnerID}
}

// LLM: Read mounted files once per immutable snapshot; errors never expose secret bytes.
// 函数用途: 读取并整体校验目录，任意非法条目都会使本次加载失败。
func Load(path string) (*Directory, error) {
	f, e := os.Open(path)
	if e != nil {
		return nil, fmt.Errorf("DIRECTORY_READ")
	}
	defer f.Close()
	var s Seed
	dec := json.NewDecoder(f)
	dec.DisallowUnknownFields()
	if dec.Decode(&s) != nil || dec.Decode(new(any)) != io.EOF {
		return nil, fmt.Errorf("DIRECTORY_JSON")
	}
	d := &Directory{Version: s.Version, cells: map[string]Cell{}, orgs: map[string]Org{}, owners: map[ownerKey]Owner{}, principals: map[string]Principal{}, apps: map[string]App{}, credentials: map[string]Credentials{}, tokens: map[string]string{}}
	if s.Version < 1 {
		return nil, fmt.Errorf("DIRECTORY_VERSION")
	}
	for _, a := range s.Apps {
		if _, exists := d.apps[a.AppID]; exists || !validID(a.AppID) || a.CredentialRef == "" || (a.Routing != "single_cell" && a.Routing != "by_owner") || (a.Routing == "single_cell" && a.CellID == nil) {
			return nil, fmt.Errorf("DIRECTORY_APP")
		}
		ek, e1 := os.ReadFile(filepath.Join(a.CredentialRef, "encrypt_key"))
		vt, e2 := os.ReadFile(filepath.Join(a.CredentialRef, "verification_token"))
		c := Credentials{strings.TrimSpace(string(ek)), strings.TrimSpace(string(vt))}
		if e1 != nil || e2 != nil || (c.EncryptKey == "" && c.VerificationToken == "") {
			return nil, fmt.Errorf("DIRECTORY_CREDENTIAL")
		}
		d.apps[a.AppID] = a
		d.credentials[a.AppID] = c
	}
	for _, c := range s.Cells {
		if _, exists := d.cells[c.CellID]; exists || !validID(c.CellID) || c.CapacityUsers < 1 || !validURL(c.BaseURL) || !validURL(c.FeishuCallbackURL) || (c.State != "serving" && c.State != "draining" && c.State != "frozen") {
			return nil, fmt.Errorf("DIRECTORY_CELL")
		}
		a, ok := d.apps[c.FeishuAppID]
		if !ok || (a.CellID != nil && *a.CellID != c.CellID) {
			return nil, fmt.Errorf("DIRECTORY_CELL_APP")
		}
		t, e := os.ReadFile(c.TokenSecretRef)
		token := strings.TrimSpace(string(t))
		if e != nil || token == "" || strings.ContainsAny(token, "\r\n") {
			return nil, fmt.Errorf("DIRECTORY_CELL_TOKEN")
		}
		d.cells[c.CellID] = c
		d.tokens[c.CellID] = token
	}
	for _, a := range s.Apps {
		if a.CellID != nil {
			c, ok := d.cells[*a.CellID]
			if !ok || c.FeishuAppID != a.AppID {
				return nil, fmt.Errorf("DIRECTORY_ONE_APP_PER_CELL")
			}
		}
	}
	for _, o := range s.Orgs {
		if o.State == "" {
			o.State = "active"
		}
		_, exists := d.orgs[o.OrgID]
		_, ok := d.cells[o.CellID]
		if exists || !ok || !validID(o.OrgID) {
			return nil, fmt.Errorf("DIRECTORY_ORG")
		}
		d.orgs[o.OrgID] = o
	}
	localHomes := map[[2]string]ownerKey{}
	for _, o := range s.Owners {
		org, ok := d.orgs[o.OrgID]
		_, exists := d.owners[key(o)]
		if !ok || exists || !validID(o.LocalOwnerID) || o.Provider != "feishu" || o.AppID != d.cells[org.CellID].FeishuAppID || o.TenantKey == "" || (o.OwnerKind != "user" && o.OwnerKind != "group") || o.ProviderOwnerID == "" || o.LocalOwnerID != o.ProviderOwnerID {
			return nil, fmt.Errorf("DIRECTORY_OWNER")
		}
		home := [2]string{org.CellID, o.LocalOwnerID}
		if previous, exists := localHomes[home]; exists && previous != key(o) {
			return nil, fmt.Errorf("DIRECTORY_OWNER_HOME_COLLISION")
		}
		localHomes[home] = key(o)
		d.owners[key(o)] = o
	}
	for _, p := range s.Principals {
		if p.State == "" {
			p.State = "active"
		}
		_, ok := d.orgs[p.OrgID]
		_, exists := d.principals[p.TokenSHA256]
		if !ok || exists || !hashPattern.MatchString(p.TokenSHA256) || !validID(p.LocalOwnerID) || (p.PrincipalKind != "feishu" && p.PrincipalKind != "web") || (p.PrincipalKind == "web" && !webPattern.MatchString(p.LocalOwnerID)) {
			return nil, fmt.Errorf("DIRECTORY_PRINCIPAL")
		}
		if p.PrincipalKind == "feishu" {
			found := false
			for _, o := range s.Owners {
				if o.OrgID == p.OrgID && o.LocalOwnerID == p.LocalOwnerID {
					found = true
					break
				}
			}
			if !found {
				return nil, fmt.Errorf("DIRECTORY_PRINCIPAL_OWNER")
			}
		}
		d.principals[p.TokenSHA256] = p
	}
	return d, nil
}

func (d *Directory) Owner(provider, appID, tenantKey, kind, id string) (Owner, Org, Cell, bool) {
	o, ok := d.owners[ownerKey{provider, appID, tenantKey, kind, id}]
	org := d.orgs[o.OrgID]
	c := d.cells[org.CellID]
	return o, org, c, ok && org.State == "active"
}
func (d *Directory) Principal(hash string) (Principal, Org, Cell, bool) {
	p, ok := d.principals[hash]
	org := d.orgs[p.OrgID]
	c := d.cells[org.CellID]
	return p, org, c, ok && p.State == "active" && org.State == "active"
}
func (d *Directory) App(id string) (App, Credentials, bool) {
	a, ok := d.apps[id]
	if a.CellID != nil {
		v := *a.CellID
		a.CellID = &v
	}
	return a, d.credentials[id], ok
}
func (d *Directory) CellToken(id string) string { return d.tokens[id] }

type Store struct {
	path        string
	current     atomic.Pointer[Directory]
	mu          sync.Mutex
	credentials map[string]Credentials
}

func NewStore(path string) (*Store, error) {
	s := &Store{path: path, credentials: map[string]Credentials{}}
	if e := s.Reload(); e != nil {
		return nil, e
	}
	return s, nil
}
func (s *Store) Current() *Directory { return s.current.Load() }

// LLM: Publish validated snapshots; app credential rotation requires a drained restart.
// 函数用途: 原子切换目录，并阻止热换应用凭据破坏已提交原始事件。
func (s *Store) Reload() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	d, e := Load(s.path)
	if e != nil {
		log.Printf("directory reload: %s", e)
		return e
	}
	for app, credential := range d.credentials {
		if previous, seen := s.credentials[app]; seen && previous != credential {
			e := fmt.Errorf("DIRECTORY_APP_CREDENTIAL_CHANGE_REQUIRES_DRAIN_RESTART")
			log.Printf("directory reload: %s", e)
			return e
		}
	}
	for app, credential := range d.credentials {
		s.credentials[app] = credential
	}
	s.current.Store(d)
	return nil
}

// LLM: Placement is append-only, capacity-aware and deterministic on equal capacity.
// 函数用途: 为新组织选择空闲容量最多的服务中 Cell，不移动已有组织。
func Assign(s Seed, org Org) (Seed, error) {
	for _, o := range s.Orgs {
		if o.OrgID == org.OrgID {
			return s, fmt.Errorf("ORG_EXISTS")
		}
	}
	used := map[string]int{}
	orgCell := map[string]string{}
	for _, o := range s.Orgs {
		orgCell[o.OrgID] = o.CellID
	}
	for _, o := range s.Owners {
		used[orgCell[o.OrgID]]++
	}
	best := ""
	free := 0
	for _, c := range s.Cells {
		n := c.CapacityUsers - used[c.CellID]
		if c.State == "serving" && n > 0 && (n > free || (n == free && c.CellID < best)) {
			best = c.CellID
			free = n
		}
	}
	if best == "" {
		return s, fmt.Errorf("CAPACITY_EXHAUSTED")
	}
	org.CellID = best
	if org.State == "" {
		org.State = "active"
	}
	s.Orgs = append(append([]Org(nil), s.Orgs...), org)
	return s, nil
}
