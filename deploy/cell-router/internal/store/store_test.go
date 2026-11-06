package store

import (
	"errors"
	"path/filepath"
	"reflect"
	"testing"
)

func TestDurableLedgerLifecycle(t *testing.T) {
	path := filepath.Join(t.TempDir(), "receipts.sqlite")
	s, e := Open(path)
	if e != nil {
		t.Fatal(e)
	}
	for key, want := range map[string]string{"journal_mode": "wal", "synchronous": "2", "foreign_keys": "1"} {
		var got string
		if e := s.db.QueryRow("PRAGMA " + key).Scan(&got); e != nil || got != want {
			t.Fatalf("%s=%s: %v", key, got, e)
		}
	}
	r := Receipt{AppID: "app-example", EventID: "event-example", RawBody: []byte(`{}`), HeadersJSON: `{}`, ReceivedAt: 1, DirectoryVersion: 1, State: "pending", NextAttemptAt: 1}
	if old, e := s.InsertReceipt(r); e != nil || old != nil {
		t.Fatal(old, e)
	}
	r.RawBody = []byte(`{"changed":true}`)
	if old, e := s.InsertReceipt(r); e != nil || old == nil || string(old.RawBody) != "{}" {
		t.Fatal(old, e)
	}
	rows, e := s.ClaimDue(1, 1)
	if e != nil || len(rows) != 1 || rows[0].Attempts != 1 {
		t.Fatal(rows, e)
	}
	if other, e := s.ClaimDue(1, 1); e != nil || len(other) != 0 {
		t.Fatal(other, e)
	}
	if e = s.Close(); e != nil {
		t.Fatal(e)
	}
	s, e = Open(path)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	if e = s.ResetDelivering(); e != nil {
		t.Fatal(e)
	}
	rows, e = s.ClaimDue(1, 1)
	if e != nil || len(rows) != 1 || rows[0].Attempts != 2 {
		t.Fatal(rows, e)
	}
	if e = s.MarkRetry(rows[0], 5, "CELL_RESPONSE"); e != nil {
		t.Fatal(e)
	}
	if rows, e = s.ClaimDue(4, 1); e != nil || len(rows) != 0 {
		t.Fatal(rows, e)
	}
	rows, e = s.ClaimDue(5, 1)
	if e != nil || len(rows) != 1 {
		t.Fatal(rows, e)
	}
	if e = s.MarkDLQ(rows[0], "RETRY_HORIZON"); e != nil {
		t.Fatal(e)
	}
	if list, e := s.ListDLQ("RETRY_HORIZON", 10); e != nil || len(list) != 1 {
		t.Fatal(list, e)
	}
	if e = s.Replay(r.AppID, r.EventID, 6); e != nil {
		t.Fatal(e)
	}
	rows, e = s.ClaimDue(6, 1)
	if e != nil || len(rows) != 1 || rows[0].Attempts != 1 {
		t.Fatal(rows, e)
	}
	if e = s.MarkDelivered(rows[0]); e != nil {
		t.Fatal(e)
	}
	if e = s.Replay(r.AppID, r.EventID, 7); e == nil {
		t.Fatal("delivered receipt replayed")
	}
	if e = s.PutCard(r.AppID, r.EventID, Card{200, []byte(`{"card":{}}`), 1}); e != nil {
		t.Fatal(e)
	}
	if c, e := s.GetCard(r.AppID, r.EventID); e != nil || c == nil || string(c.Body) != `{"card":{}}` {
		t.Fatal(c, e)
	}
	for _, state := range []string{"pending", "delivering", "dlq"} {
		copy := r
		copy.EventID = state
		copy.State = state
		if _, e = s.InsertReceipt(copy); e != nil {
			t.Fatal(e)
		}
	}
	if e = s.PurgeBefore(10); e != nil {
		t.Fatal(e)
	}
	if _, e = s.GetReceipt(r.AppID, r.EventID); e != ErrNotFound {
		t.Fatal(e)
	}
	if c, e := s.GetCard(r.AppID, r.EventID); e != nil || c != nil {
		t.Fatal(c, e)
	}
	for _, state := range []string{"pending", "delivering", "dlq"} {
		if _, e = s.GetReceipt(r.AppID, state); e != nil {
			t.Fatal("purged live or DLQ row", state, e)
		}
	}
}

// LLM: Replay may reset scheduling only for DLQ; receipt identity, bytes and original age remain durable.
// 函数用途: 验证只有死信可重放，清零后首次领取为一次，且去重不覆盖历史数据。
func TestReplayPreservesReceiptIdentityAndAge(t *testing.T) {
	for _, state := range []string{"pending", "delivering", "delivered", "dlq"} {
		t.Run(state, func(t *testing.T) {
			s, e := Open(filepath.Join(t.TempDir(), "receipts.sqlite"))
			if e != nil {
				t.Fatal(e)
			}
			defer s.Close()
			target := "cell-00"
			r := Receipt{AppID: "app-example", EventID: "aged-example", RawBody: []byte(`{"original":true}`), HeadersJSON: `{"Content-Type":["application/json"]}`, ReceivedAt: 1, DirectoryVersion: 7, TargetCell: &target, State: state, Attempts: 9, NextAttemptAt: 5, LastError: "RETRY_HORIZON"}
			if old, e := s.InsertReceipt(r); e != nil || old != nil {
				t.Fatal(old, e)
			}
			const replayAt int64 = 99_000_000
			e = s.Replay(r.AppID, r.EventID, replayAt)
			if state != "dlq" {
				if !errors.Is(e, ErrNotFound) {
					t.Fatal("non-DLQ receipt replayed", e)
				}
				row, e := s.GetReceipt(r.AppID, r.EventID)
				if e != nil || !reflect.DeepEqual(row, &r) {
					t.Fatal("refused replay changed receipt", row, e)
				}
				return
			}
			if e != nil {
				t.Fatal(e)
			}
			want := r
			want.State, want.Attempts, want.NextAttemptAt, want.LastError = "pending", 0, replayAt, ""
			row, e := s.GetReceipt(r.AppID, r.EventID)
			if e != nil || !reflect.DeepEqual(row, &want) {
				t.Fatal("replay changed more than scheduling", row, e)
			}
			duplicate := r
			duplicate.RawBody, duplicate.ReceivedAt, duplicate.DirectoryVersion = []byte(`{"replacement":true}`), replayAt, 8
			if old, e := s.InsertReceipt(duplicate); e != nil || !reflect.DeepEqual(old, &want) {
				t.Fatal("duplicate changed replayed receipt", old, e)
			}
			if rows, e := s.ClaimDue(replayAt-1, 1); e != nil || len(rows) != 0 {
				t.Fatal("replay scheduled before requested time", rows, e)
			}
			want.State, want.Attempts = "delivering", 1
			if rows, e := s.ClaimDue(replayAt, 1); e != nil || len(rows) != 1 || !reflect.DeepEqual(&rows[0], &want) {
				t.Fatal("replay claim did not start at attempt one", rows, e)
			}
			if rows, e := s.ClaimDue(replayAt, 1); e != nil || len(rows) != 0 {
				t.Fatal("replay claim was not exclusive", rows, e)
			}
		})
	}
}
