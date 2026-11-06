package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestLoadDefaultsAndValidation(t *testing.T) {
	for _, tc := range []struct {
		name, body string
		bad        bool
	}{
		{"defaults", `{"directory_seed_path":"seed.json","receipts_db_path":"receipts.sqlite"}`, false},
		{"override", `{"directory_seed_path":"seed.json","receipts_db_path":"receipts.sqlite","forward_workers":2,"card_proxy_timeout":"1500ms"}`, false},
		{"unknown", `{"directory_seed_path":"seed.json","receipts_db_path":"receipts.sqlite","unexpected":1}`, true},
		{"missing", `{}`, true}, {"duration", `{"directory_seed_path":"s","receipts_db_path":"r","forward_timeout":"0s"}`, true},
		{"negative", `{"directory_seed_path":"s","receipts_db_path":"r","forward_workers":-1}`, true},
		{"trailing", `{} {}`, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := filepath.Join(t.TempDir(), "config.json")
			if e := os.WriteFile(p, []byte(tc.body), 0600); e != nil {
				t.Fatal(e)
			}
			c, e := Load(p)
			if (e != nil) != tc.bad {
				t.Fatalf("load: %v", e)
			}
			if e == nil {
				b, _ := json.Marshal(c)
				var round Config
				if e = json.Unmarshal(b, &round); e != nil || c != round {
					t.Fatalf("config roundtrip: %v", e)
				}
			}
		})
	}
}
