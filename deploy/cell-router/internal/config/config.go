// Package config owns all operator policy for the router process.
package config

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"time"
)

type Duration struct{ time.Duration }

func (d Duration) MarshalJSON() ([]byte, error) { return json.Marshal(d.String()) }
func (d *Duration) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	v, err := time.ParseDuration(s)
	if err == nil {
		d.Duration = v
	}
	return err
}

type Config struct {
	ListenAddr             string   `json:"listen_addr"`
	AdminAddr              string   `json:"admin_addr"`
	DirectorySeedPath      string   `json:"directory_seed_path"`
	ReceiptsDBPath         string   `json:"receipts_db_path"`
	ReadHeaderTimeout      Duration `json:"read_header_timeout"`
	TimestampTolerance     Duration `json:"timestamp_tolerance"`
	ForwardTimeout         Duration `json:"forward_timeout"`
	ForwardWorkers         int      `json:"forward_workers"`
	ForwardBackoffInitial  Duration `json:"forward_backoff_initial"`
	ForwardBackoffMax      Duration `json:"forward_backoff_max"`
	ForwardRetryHorizon    Duration `json:"forward_retry_horizon"`
	CardProxyTimeout       Duration `json:"card_proxy_timeout"`
	ReceiptRetention       Duration `json:"receipt_retention"`
	ShutdownTimeout        Duration `json:"shutdown_timeout"`
	MaxHeaderBytes         int      `json:"max_header_bytes"`
	MaxBodyBytes           int64    `json:"max_body_bytes"`
	MaxResponseBytes       int64    `json:"max_response_bytes"`
	AdminListLimit         int      `json:"admin_list_limit"`
	HTTPMaxIdleConnections int      `json:"http_max_idle_connections"`
	HTTPIdleTimeout        Duration `json:"http_idle_timeout"`
}

// LLM: Composition-root defaults only; handlers receive this resolved value.
// 函数用途: 集中定义路由器的运维参数默认值。
func Defaults() Config {
	return Config{
		ListenAddr: ":8080", AdminAddr: ":8081",
		ReadHeaderTimeout: Duration{5 * time.Second}, TimestampTolerance: Duration{300 * time.Second},
		ForwardTimeout: Duration{10 * time.Second}, ForwardWorkers: 8,
		ForwardBackoffInitial: Duration{time.Second}, ForwardBackoffMax: Duration{60 * time.Second},
		ForwardRetryHorizon: Duration{30 * time.Minute}, CardProxyTimeout: Duration{2500 * time.Millisecond},
		ReceiptRetention: Duration{24 * time.Hour}, ShutdownTimeout: Duration{25 * time.Second},
		MaxHeaderBytes: 64 * 1024, MaxBodyBytes: 4 * 1024 * 1024, MaxResponseBytes: 4 * 1024 * 1024,
		AdminListLimit: 100, HTTPMaxIdleConnections: 128, HTTPIdleTimeout: Duration{60 * time.Second},
	}
}

// LLM: Reject unknown fields, trailing JSON, invalid bounds and missing storage paths.
// 函数用途: 在启动前严格解析配置并保留所有有效默认值。
func Load(path string) (Config, error) {
	c := Defaults()
	f, err := os.Open(path)
	if err != nil {
		return c, err
	}
	defer f.Close()
	d := json.NewDecoder(f)
	d.DisallowUnknownFields()
	if err = d.Decode(&c); err != nil {
		return c, err
	}
	if err = d.Decode(new(any)); err != io.EOF {
		return c, fmt.Errorf("CONFIG_TRAILING_JSON")
	}
	if c.ListenAddr == "" || c.AdminAddr == "" || c.ListenAddr == c.AdminAddr || c.DirectorySeedPath == "" || c.ReceiptsDBPath == "" {
		return c, fmt.Errorf("CONFIG_REQUIRED")
	}
	for _, v := range []time.Duration{c.ReadHeaderTimeout.Duration, c.TimestampTolerance.Duration, c.ForwardTimeout.Duration, c.ForwardBackoffInitial.Duration, c.ForwardBackoffMax.Duration, c.ForwardRetryHorizon.Duration, c.CardProxyTimeout.Duration, c.ReceiptRetention.Duration, c.ShutdownTimeout.Duration, c.HTTPIdleTimeout.Duration} {
		if v <= 0 {
			return c, fmt.Errorf("CONFIG_DURATION")
		}
	}
	if c.ForwardWorkers <= 0 || c.MaxHeaderBytes <= 0 || c.MaxBodyBytes <= 0 || c.MaxResponseBytes <= 0 || c.AdminListLimit <= 0 || c.HTTPMaxIdleConnections <= 0 || c.ForwardBackoffInitial.Duration > c.ForwardBackoffMax.Duration {
		return c, fmt.Errorf("CONFIG_RANGE")
	}
	return c, nil
}
