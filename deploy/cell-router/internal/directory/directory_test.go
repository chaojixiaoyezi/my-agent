package directory_test

import (
	"cellrouter/internal/directory"
	"cellrouter/internal/testutil"
	"fmt"
	"strings"
	"testing"
)

func TestCorpusAndAppendOnlyPlacement(t *testing.T) {
	e := testutil.New(t, 16, "http://cell-a.example.internal:8420", false)
	for i := 0; i < 2000; i++ {
		n := i % 16
		_, org, cell, ok := e.Directory.Current().Owner("feishu", "app-example", "tenant-example", "user", fmt.Sprintf("ou_example_%02d", n))
		if !ok || org.OrgID != fmt.Sprintf("org-%02d", n) || cell.CellID != fmt.Sprintf("cell-%02d", n) {
			t.Fatalf("corpus event %d: %v %v", i, org, cell)
		}
	}
	s := e.Seed
	extra := s.Cells[0]
	extra.CellID = "cell-17"
	extra.CapacityUsers = 1001
	s.Cells = append(s.Cells, extra)
	assigned, err := directory.Assign(s, directory.Org{OrgID: "org-new"})
	if err != nil {
		t.Fatal(err)
	}
	if assigned.Orgs[len(s.Orgs)].CellID != "cell-17" {
		t.Fatal("capacity choice")
	}
	for i, o := range s.Orgs {
		if assigned.Orgs[i] != o {
			t.Fatal("existing organisation moved")
		}
	}
}
func TestCompositeOwnerIdentity(t *testing.T) {
	e := testutil.New(t, 2, "http://cell-a.example.internal:8420", false)
	e.Seed.Cells[1].FeishuAppID = "app-other"
	a := e.Seed.Apps[0]
	a.AppID = "app-other"
	e.Seed.Apps = append(e.Seed.Apps, a)
	e.Seed.Owners[1].AppID = "app-other"
	e.Seed.Owners[1].TenantKey = "tenant-other-example"
	e.Seed.Owners[1].ProviderOwnerID = e.Seed.Owners[0].ProviderOwnerID
	e.Seed.Owners[1].LocalOwnerID = e.Seed.Owners[0].LocalOwnerID
	e.Save(t)
	if err := e.Directory.Reload(); err != nil {
		t.Fatal(err)
	}
	_, _, aCell, aOK := e.Directory.Current().Owner("feishu", "app-example", "tenant-example", "user", e.Seed.Owners[0].LocalOwnerID)
	_, _, bCell, bOK := e.Directory.Current().Owner("feishu", "app-other", "tenant-other-example", "user", e.Seed.Owners[0].LocalOwnerID)
	if !aOK || !bOK || aCell.CellID == bCell.CellID {
		t.Fatal("owner identity collapsed")
	}
}
func TestInvalidReloadKeepsSnapshot(t *testing.T) {
	for _, name := range []string{"two-apps", "mixed-credentials", "admin", "hash", "owner-org", "org-cell", "local-mismatch", "enum", "web-id", "single-cell"} {
		t.Run(name, func(t *testing.T) {
			e := testutil.New(t, 1, "http://cell-a.example.internal:8420", false)
			old := e.Directory.Current()
			e.Seed.Version++
			switch name {
			case "two-apps":
				a := e.Seed.Apps[0]
				a.AppID = "app-other"
				id := e.Seed.Cells[0].CellID
				a.CellID = &id
				e.Seed.Apps = append(e.Seed.Apps, a)
			case "mixed-credentials":
				a := e.Seed.Apps[0]
				a.CredentialRef = t.TempDir()
				e.Seed.Apps = append(e.Seed.Apps, a)
			case "admin":
				e.Seed.Owners[0].LocalOwnerID = "admin"
			case "hash":
				e.Seed.Principals[0].TokenSHA256 = strings.Repeat("z", 64)
			case "owner-org":
				e.Seed.Owners[0].OrgID = "not-listed"
			case "org-cell":
				e.Seed.Orgs[0].CellID = "not-listed"
			case "local-mismatch":
				e.Seed.Owners[0].LocalOwnerID = "different"
			case "enum":
				e.Seed.Cells[0].State = "other"
			case "web-id":
				e.Seed.Principals[0].PrincipalKind = "web"
			case "single-cell":
				e.Seed.Apps[0].Routing = "single_cell"
			}
			e.Save(t)
			if e.Directory.Reload() == nil {
				t.Fatal("invalid seed accepted")
			}
			if e.Directory.Current() != old {
				t.Fatal("invalid snapshot published")
			}
		})
	}
}
