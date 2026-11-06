// Package store is the durable, single-writer Feishu delivery ledger.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	_ "modernc.org/sqlite"
	"net/url"
	"time"
)

const receiptDDL = "CREATE TABLE feishu_receipts (app_id TEXT NOT NULL, event_id TEXT NOT NULL, raw_body BLOB NOT NULL, headers_json TEXT NOT NULL, received_at INTEGER NOT NULL, directory_version INTEGER NOT NULL, target_cell TEXT NULL, state TEXT NOT NULL CHECK (state IN ('pending','delivering','delivered','dlq')), attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL, last_error TEXT NOT NULL DEFAULT '', PRIMARY KEY (app_id, event_id))"
const columns = "app_id,event_id,raw_body,headers_json,received_at,directory_version,target_cell,state,attempts,next_attempt_at,last_error"

var ErrNotFound = errors.New("RECEIPT_NOT_FOUND")

type Receipt struct {
	AppID            string  `json:"app_id"`
	EventID          string  `json:"event_id"`
	RawBody          []byte  `json:"-"`
	HeadersJSON      string  `json:"-"`
	ReceivedAt       int64   `json:"received_at"`
	DirectoryVersion int64   `json:"directory_version"`
	TargetCell       *string `json:"target_cell"`
	State            string  `json:"state"`
	Attempts         int     `json:"attempts"`
	NextAttemptAt    int64   `json:"next_attempt_at"`
	LastError        string  `json:"last_error"`
}
type Card struct {
	Status    int
	Body      []byte
	CreatedAt int64
}
type Store struct{ db *sql.DB }

// LLM: One retained connection keeps FULL/WAL pragmas effective for all transactions.
// 函数用途: 打开同步持久化的回执数据库并初始化冻结表结构。
func Open(path string) (*Store, error) {
	u := url.URL{Scheme: "file", Path: path}
	db, e := sql.Open("sqlite", u.String())
	if e != nil {
		return nil, e
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	for _, q := range []string{"PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL", "PRAGMA foreign_keys=ON"} {
		if _, e = db.Exec(q); e != nil {
			db.Close()
			return nil, e
		}
	}
	var n int
	e = db.QueryRow("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='feishu_receipts'").Scan(&n)
	if e == nil && n == 0 {
		_, e = db.Exec(receiptDDL)
	}
	if e != nil {
		db.Close()
		return nil, e
	}
	_, e = db.Exec("CREATE TABLE IF NOT EXISTS card_results(app_id TEXT, event_id TEXT, status INTEGER, body BLOB, created_at INTEGER, PRIMARY KEY(app_id, event_id)); CREATE INDEX IF NOT EXISTS receipts_due ON feishu_receipts(state,next_attempt_at)")
	if e != nil {
		db.Close()
		return nil, e
	}
	return &Store{db}, nil
}
func (s *Store) Close() error { return s.db.Close() }

type scanner interface{ Scan(...any) error }

func scan(row scanner) (*Receipt, error) {
	var r Receipt
	e := row.Scan(&r.AppID, &r.EventID, &r.RawBody, &r.HeadersJSON, &r.ReceivedAt, &r.DirectoryVersion, &r.TargetCell, &r.State, &r.Attempts, &r.NextAttemptAt, &r.LastError)
	if errors.Is(e, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return &r, e
}

// LLM: Return only after COMMIT; existing is non-nil only for a duplicate primary key.
// 函数用途: 在单次事务内写入并读回回执，提交失败必须阻止平台 ACK。
func (s *Store) InsertReceipt(r Receipt) (existing *Receipt, err error) {
	tx, e := s.db.BeginTx(context.Background(), nil)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback()
	res, e := tx.Exec("INSERT INTO feishu_receipts ("+columns+") VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(app_id,event_id) DO NOTHING", r.AppID, r.EventID, r.RawBody, r.HeadersJSON, r.ReceivedAt, r.DirectoryVersion, r.TargetCell, r.State, r.Attempts, r.NextAttemptAt, r.LastError)
	if e != nil {
		return nil, e
	}
	count, e := res.RowsAffected()
	if e != nil {
		return nil, e
	}
	read, e := scan(tx.QueryRow("SELECT "+columns+" FROM feishu_receipts WHERE app_id=? AND event_id=?", r.AppID, r.EventID))
	if e != nil {
		return nil, e
	}
	if e = tx.Commit(); e != nil {
		return nil, e
	}
	if count == 0 {
		return read, nil
	}
	return nil, nil
}
func (s *Store) GetReceipt(app, id string) (*Receipt, error) {
	return scan(s.db.QueryRow("SELECT "+columns+" FROM feishu_receipts WHERE app_id=? AND event_id=?", app, id))
}
func (s *Store) ResetDelivering() error {
	_, e := s.db.Exec("UPDATE feishu_receipts SET state='pending' WHERE state='delivering'")
	return e
}

// LLM: Claim and attempt increment share a transaction; workers cannot claim the same row.
// 函数用途: 原子领取到期回执，并为每次尝试递增计数。
func (s *Store) ClaimDue(now int64, limit int) ([]Receipt, error) {
	tx, e := s.db.Begin()
	if e != nil {
		return nil, e
	}
	defer tx.Rollback()
	rows, e := tx.Query("SELECT "+columns+" FROM feishu_receipts WHERE state='pending' AND next_attempt_at<=? ORDER BY next_attempt_at,received_at LIMIT ?", now, limit)
	if e != nil {
		return nil, e
	}
	result := []Receipt{}
	for rows.Next() {
		r, e := scan(rows)
		if e != nil {
			rows.Close()
			return nil, e
		}
		result = append(result, *r)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	for i := range result {
		r := &result[i]
		_, e = tx.Exec("UPDATE feishu_receipts SET state='delivering',attempts=attempts+1 WHERE app_id=? AND event_id=? AND state='pending'", r.AppID, r.EventID)
		if e != nil {
			return nil, e
		}
		r.State = "delivering"
		r.Attempts++
	}
	if e = tx.Commit(); e != nil {
		return nil, e
	}
	return result, nil
}
func (s *Store) NextDue() (int64, bool, error) {
	var n sql.NullInt64
	e := s.db.QueryRow("SELECT min(next_attempt_at) FROM feishu_receipts WHERE state='pending'").Scan(&n)
	return n.Int64, n.Valid, e
}
func (s *Store) SetTarget(r Receipt, cell string) error {
	_, e := s.db.Exec("UPDATE feishu_receipts SET target_cell=? WHERE app_id=? AND event_id=? AND state='delivering'", cell, r.AppID, r.EventID)
	return e
}
func (s *Store) MarkDelivered(r Receipt) error {
	return s.transition(r, "delivered", r.NextAttemptAt, "")
}
func (s *Store) MarkRetry(r Receipt, next int64, reason string) error {
	return s.transition(r, "pending", next, reason)
}
func (s *Store) MarkDLQ(r Receipt, reason string) error {
	return s.transition(r, "dlq", r.NextAttemptAt, reason)
}
func (s *Store) transition(r Receipt, state string, next int64, reason string) error {
	res, e := s.db.Exec("UPDATE feishu_receipts SET state=?,next_attempt_at=?,last_error=? WHERE app_id=? AND event_id=? AND state='delivering'", state, next, reason, r.AppID, r.EventID)
	if e != nil {
		return e
	}
	n, e := res.RowsAffected()
	if e == nil && n != 1 {
		return fmt.Errorf("RECEIPT_STATE")
	}
	return e
}
func (s *Store) ListDLQ(reason string, limit int) ([]Receipt, error) {
	rows, e := s.db.Query("SELECT "+columns+" FROM feishu_receipts WHERE state='dlq' AND (?='' OR last_error=?) ORDER BY received_at LIMIT ?", reason, reason, limit)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	result := []Receipt{}
	for rows.Next() {
		r, e := scan(rows)
		if e != nil {
			return nil, e
		}
		result = append(result, *r)
	}
	return result, rows.Err()
}

// LLM: Only DLQ is replayable; delivered records cannot move backwards through this API.
// 函数用途: 显式重放死信，重置尝试计数并立即调度。
func (s *Store) Replay(app, id string, now int64) error {
	res, e := s.db.Exec("UPDATE feishu_receipts SET state='pending',attempts=0,next_attempt_at=?,last_error='' WHERE app_id=? AND event_id=? AND state='dlq'", now, app, id)
	if e != nil {
		return e
	}
	n, e := res.RowsAffected()
	if e == nil && n != 1 {
		return ErrNotFound
	}
	return e
}
func (s *Store) PurgeOlderThan(retention time.Duration) error {
	return s.PurgeBefore(time.Now().Add(-retention).UnixMilli())
}
func (s *Store) PurgeBefore(cutoff int64) error {
	tx, e := s.db.Begin()
	if e != nil {
		return e
	}
	defer tx.Rollback()
	if _, e = tx.Exec("DELETE FROM feishu_receipts WHERE state='delivered' AND received_at<?", cutoff); e != nil {
		return e
	}
	if _, e = tx.Exec("DELETE FROM card_results WHERE created_at<?", cutoff); e != nil {
		return e
	}
	return tx.Commit()
}
func (s *Store) GetCard(app, id string) (*Card, error) {
	var c Card
	e := s.db.QueryRow("SELECT status,body,created_at FROM card_results WHERE app_id=? AND event_id=?", app, id).Scan(&c.Status, &c.Body, &c.CreatedAt)
	if errors.Is(e, sql.ErrNoRows) {
		return nil, nil
	}
	return &c, e
}
func (s *Store) PutCard(app, id string, c Card) error {
	_, e := s.db.Exec("INSERT INTO card_results(app_id,event_id,status,body,created_at) VALUES(?,?,?,?,?) ON CONFLICT(app_id,event_id) DO NOTHING", app, id, c.Status, c.Body, c.CreatedAt)
	return e
}
