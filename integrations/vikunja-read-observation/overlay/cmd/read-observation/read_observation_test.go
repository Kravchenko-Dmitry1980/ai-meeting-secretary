//go:build windows

// Vikunja is a to-do list application to facilitate your life.
// Copyright 2018-present Vikunja and contributors. All rights reserved.
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program. If not, see <https://www.gnu.org/licenses/>.

package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"code.vikunja.io/api/pkg/config"
	"code.vikunja.io/api/pkg/db"
	"code.vikunja.io/api/pkg/license"
	"code.vikunja.io/api/pkg/log"
	"code.vikunja.io/api/pkg/models"
	"code.vikunja.io/api/pkg/modules/auth"
	"code.vikunja.io/api/pkg/modules/keyvalue"
	apiv2 "code.vikunja.io/api/pkg/routes/api/v2"
	"code.vikunja.io/api/pkg/user"
	"code.vikunja.io/api/pkg/web"
	"code.vikunja.io/api/pkg/web/handler"

	"xorm.io/xorm"
	"xorm.io/xorm/names"
)

const (
	observationTestProjectID = int64(201)
	observationTestDeniedProjectID = int64(202)
	observationTestPrincipalID = int64(101)
	observationTestHumanID = int64(1)
	observationTestShares = 121
)

type observationTestFixture struct {
	path string
	before []byte
	tokens map[string]string
}

// This package has no TestMain. The runtime/test initializer closure must be
// approved before running it; importing a package does not run its test files.
// Every writable operation below belongs to one new t.TempDir fixture and
// completes before that fixture is opened by the read-only profile.
func TestReadObservation(t *testing.T) {
	if err := config.InitReadObservation(); err != nil {
		t.Fatal("fixed configuration initialization failed")
	}
	log.ConfigureStandardLogger(false, "off", t.TempDir(), "ERROR", "text")
	keyvalue.InitStorage()
	if _, exists, err := keyvalue.Get("license.state"); err != nil || exists {
		t.Fatal("fresh memory storage has unexpected license state")
	}

	t.Run("refusal-before-engine-or-state", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		if err := license.InitReadObservation(nil); err == nil {
			t.Fatal("nil license context was accepted")
		}
		if err := license.InitReadObservation(ctx); !errors.Is(err, context.Canceled) {
			t.Fatal("canceled license context was not propagated")
		}
		if session, err := db.NewReadObservationReadSession(ctx); session != nil || !errors.Is(err, context.Canceled) {
			t.Fatal("canceled read session was not refused")
		}
		if session, err := db.NewReadObservationTransaction(ctx); session != nil || !errors.Is(err, context.Canceled) {
			t.Fatal("canceled transaction was not refused")
		}
		if token, err := models.GetReadObservationAPIToken(ctx, "tk_synthetic00"); token != nil || !errors.Is(err, context.Canceled) {
			t.Fatal("canceled token lookup was not refused")
		}
		if token, principal, err := auth.ValidateReadObservation(ctx, "tk_synthetic00", observationBinding(observationTestProjectID)); token != nil || principal != nil || !errors.Is(err, context.Canceled) {
			t.Fatal("canceled authentication was not refused")
		}
		config.LicenseKey.Set("synthetic-license-precheck-only")
		if err := license.InitReadObservation(context.Background()); err == nil {
			t.Fatal("nonempty license key was accepted")
		}
		config.LicenseKey.Set("")
		config.KeyvalueType.Set("redis")
		if err := license.InitReadObservation(context.Background()); err == nil {
			t.Fatal("nonmemory configuration was accepted")
		}
		config.KeyvalueType.Set("memory")
		if _, exists, err := keyvalue.Get("license.state"); err != nil || exists {
			t.Fatal("precheck refusal created license state")
		}
	})

	t.Run("database-preflight", observationDatabasePreflight)
	for _, candidate := range []struct {
		name string
		statuses []license.Status
	}{
		{name: "missing-license-instance"},
		{name: "empty-license-instance", statuses: []license.Status{{ID: 1, InstanceID: "", Response: "{}"}}},
		{name: "multiple-license-instances", statuses: []license.Status{
			{ID: 1, InstanceID: "11111111-1111-4111-8111-111111111111", Response: "{}"},
			{ID: 2, InstanceID: "22222222-2222-4222-8222-222222222222", Response: "{}"},
		}},
	} {
		t.Run(candidate.name, func(t *testing.T) {
			fixture := observationFixture(t, candidate.statuses, "")
			withObservationEngine(t, fixture, func(*xorm.Engine) {
				if err := license.InitReadObservation(context.Background()); err == nil {
					t.Fatal("invalid existing license status was accepted")
				}
				if _, exists, err := keyvalue.Get("license.state"); err != nil || exists {
					t.Fatal("invalid license status created cached state")
				}
			})
		})
	}

	// The only successful initializer in the process uses one existing row;
	// failed prechecks above leave the fresh private license state untouched.
	fixture := observationFixture(t, []license.Status{{
		ID: 1,
		InstanceID: "33333333-3333-4333-8333-333333333333",
		Response: "{}",
	}}, "")
	withObservationEngine(t, fixture, func(engine *xorm.Engine) {
		t.Run("genuine-free-state", func(t *testing.T) {
			if err := license.InitReadObservation(context.Background()); err != nil {
				t.Fatal("existing FREE initialization failed")
			}
			if raw, exists, err := keyvalue.Get("license.state"); err != nil || !exists || raw == nil {
				t.Fatal("FREE initializer did not confirm stored state")
			}
			if features := license.EnabledProFeatures(); features == nil || len(features) != 0 {
				t.Fatal("FREE mode exposes paid features")
			}
			for _, feature := range []license.Feature{
				license.FeatureAdminPanel,
				license.FeatureTimeTracking,
				license.FeatureAuditLogs,
				license.FeatureUserInvites,
			} {
				if license.IsFeatureEnabled(feature) {
					t.Fatal("FREE mode enabled a paid feature")
				}
			}
			if license.MaxUsersReached() {
				t.Fatal("FREE mode imposed a paid user limit")
			}
			if err := license.InitReadObservation(context.Background()); err == nil {
				t.Fatal("duplicate license initialization was accepted")
			}
		})
		t.Run("modern-authentication-and-bindings", func(t *testing.T) {
			observationAuthentication(t, fixture)
		})
		t.Run("native-http-read-contracts", func(t *testing.T) {
			observationNativeHTTP(t, fixture)
		})
		t.Run("canonical-http-gate-before-auth", func(t *testing.T) {
			observationHTTPGates(t, fixture)
		})
		t.Run("late-native-cancellation", observationLateCancellation)
		t.Run("read-only-engine-refuses-model-write", func(t *testing.T) {
			if _, err := engine.Insert(&models.Project{Title: "synthetic-forbidden-write", OwnerID: observationTestHumanID}); err == nil {
				t.Fatal("read-only engine accepted a model insert")
			}
		})
	})

	t.Run("native-response-byte-caps", observationResponseCaps)
}

func observationBinding(projectID int64) auth.ReadObservationBinding {
	return auth.ReadObservationBinding{
		ProjectID: projectID,
		PrincipalID: observationTestPrincipalID,
		HumanOwnerID: observationTestHumanID,
	}
}

func observationFixture(t *testing.T, statuses []license.Status, description string) observationTestFixture {
	t.Helper()
	path := filepath.Join(t.TempDir(), "synthetic.sqlite")
	dsn := (&url.URL{
		Scheme: "file",
		Path: "/"+filepath.ToSlash(path),
		RawQuery: "mode=rwc&cache=private&_busy_timeout=1000",
	}).String()
	engine, err := xorm.NewEngine("sqlite3", dsn)
	if err != nil || engine == nil {
		if engine != nil {
			if closeErr := engine.Close(); closeErr != nil {
				t.Error("failed own fixture engine cleanup")
			}
		}
		t.Fatal("own fixture engine creation failed")
	}
	closed := false
	defer func() {
		if !closed {
			if err := engine.Close(); err != nil {
				t.Error("own fixture emergency close failed")
			}
		}
	}()
	engine.SetLogger(log.NewXormLogger(false, "off", "ERROR", "text"))
	engine.SetMapper(names.GonicMapper{})
	engine.SetTZLocation(time.UTC)
	engine.SetTZDatabase(time.UTC)
	engine.SetMaxOpenConns(1)
	engine.SetMaxIdleConns(1)
	beans := append(models.GetTables(), user.GetTables()...)
	beans = append(beans, license.GetTables()...)
	if err := engine.Sync2(beans...); err != nil {
		t.Fatal("own fixture schema preparation failed")
	}
	users := []user.User{
		{ID: 1, Username: "synthetic-human", Name: "Synthetic human", Issuer: user.IssuerLocal, Status: user.StatusActive},
		{ID: 2, Username: "synthetic-other-human", Issuer: user.IssuerLocal, Status: user.StatusActive},
		{ID: 4, Username: "synthetic-disabled-human", Issuer: user.IssuerLocal, Status: user.StatusDisabled},
		{ID: 5, Username: "synthetic-nonlocal-human", Issuer: "synthetic-oidc", Status: user.StatusActive},
		{ID: 6, Username: "bot-synthetic-human", Issuer: user.IssuerLocal, Status: user.StatusActive, BotOwnerID: 1},
		{ID: 101, Username: "bot-synthetic-reader", Name: "Synthetic reader", Issuer: user.IssuerLocal, Status: user.StatusActive, BotOwnerID: 1, IsAdmin: true},
		{ID: 102, Username: "bot-synthetic-disabled", Issuer: user.IssuerLocal, Status: user.StatusDisabled, BotOwnerID: 1},
		{ID: 103, Username: "bot-synthetic-locked", Issuer: user.IssuerLocal, Status: user.StatusAccountLocked, BotOwnerID: 1},
		{ID: 104, Username: "bot-synthetic-nonlocal", Issuer: "synthetic-oidc", Status: user.StatusActive, BotOwnerID: 1},
		{ID: 105, Username: "bot-synthetic-other-owner", Issuer: user.IssuerLocal, Status: user.StatusActive, BotOwnerID: 2},
		{ID: 106, Username: "synthetic-not-bot", Issuer: user.IssuerLocal, Status: user.StatusActive},
		{ID: 107, Username: "bot-synthetic-disabled-owner", Issuer: user.IssuerLocal, Status: user.StatusActive, BotOwnerID: 4},
		{ID: 108, Username: "bot-synthetic-nonlocal-owner", Issuer: user.IssuerLocal, Status: user.StatusActive, BotOwnerID: 5},
		{ID: 109, Username: "bot-synthetic-bot-owner", Issuer: user.IssuerLocal, Status: user.StatusActive, BotOwnerID: 6},
	}
	shares := []models.ProjectUser{{ProjectID: observationTestProjectID, UserID: observationTestPrincipalID, Permission: models.PermissionRead}}
	for n := 0; n < observationTestShares-1; n++ {
		id := int64(1000+n)
		users = append(users, user.User{ID: id, Username: fmt.Sprintf("synthetic-member-%03d", n), Issuer: user.IssuerLocal, Status: user.StatusActive, Email: "synthetic@example.invalid"})
		shares = append(shares, models.ProjectUser{ProjectID: observationTestProjectID, UserID: id, Permission: models.PermissionRead})
	}
	if _, err := engine.Insert(&users); err != nil {
		t.Fatal("own fixture users preparation failed")
	}
	projects := []models.Project{
		{ID: observationTestProjectID, Title: "Synthetic accessible project", OwnerID: 1, Description: description},
		{ID: observationTestDeniedProjectID, Title: "Synthetic inaccessible project", OwnerID: 2},
	}
	if _, err := engine.Insert(&projects); err != nil {
		t.Fatal("own fixture projects preparation failed")
	}
	if _, err := engine.Insert(&shares); err != nil {
		t.Fatal("own fixture direct shares preparation failed")
	}
	if len(statuses) > 0 {
		if _, err := engine.Insert(&statuses); err != nil {
			t.Fatal("own fixture license statuses preparation failed")
		}
	}
	tokens := make(map[string]string)
	permissions := models.APIPermissions{
		"other": {"user"},
		"projects": {"read_one"},
		"projects_users": {"read_all"},
	}
	for _, candidate := range []struct {
		name string
		ownerID int64
		expired bool
		permissions models.APIPermissions
		legacy bool
	}{
		{name: "valid", ownerID: 101, permissions: permissions},
		{name: "expired", ownerID: 101, permissions: permissions, expired: true},
		{name: "disabled", ownerID: 102, permissions: permissions},
		{name: "locked", ownerID: 103, permissions: permissions},
		{name: "nonlocal", ownerID: 104, permissions: permissions},
		{name: "wrong-bot-owner", ownerID: 105, permissions: permissions},
		{name: "not-bot", ownerID: 106, permissions: permissions},
		{name: "disabled-human", ownerID: 107, permissions: permissions},
		{name: "nonlocal-human", ownerID: 108, permissions: permissions},
		{name: "bot-human", ownerID: 109, permissions: permissions},
		{name: "missing-principal", ownerID: 99999, permissions: permissions},
		{name: "no-scopes", ownerID: 101, permissions: models.APIPermissions{}},
		{name: "project-only", ownerID: 101, permissions: models.APIPermissions{"projects": {"read_one"}}},
		{name: "legacy", ownerID: 101, permissions: permissions, legacy: true},
	} {
		rawBytes := make([]byte, 20)
		if _, err := rand.Read(rawBytes); err != nil {
			t.Fatal("synthetic token entropy unavailable")
		}
		raw := models.APITokenPrefix + hex.EncodeToString(rawBytes)
		tokens[candidate.name] = raw
		expiry := time.Now().UTC().Add(time.Hour)
		if candidate.expired {
			expiry = time.Now().UTC().Add(-time.Hour)
		}
		token := &models.APIToken{
			Title: "synthetic-"+candidate.name,
			OwnerID: candidate.ownerID,
			ExpiresAt: expiry,
			APIPermissions: candidate.permissions,
			TokenSha256: models.HashAPIToken(raw),
		}
		if candidate.legacy {
			token.TokenSha256 = ""
			token.TokenSalt = "synthetic-legacy-salt"
			token.TokenHash = models.HashToken(raw, token.TokenSalt)
			token.TokenLastEight = raw[len(raw)-8:]
		}
		session := engine.NewSession()
		var insertErr error
		if candidate.legacy {
			_, insertErr = session.Nullable("token_sha256").Insert(token)
		} else {
			_, insertErr = session.Nullable("token_salt", "token_hash", "token_last_eight").Insert(token)
		}
		closeErr := session.Close()
		if insertErr != nil || closeErr != nil {
			t.Fatal("own fixture token preparation failed")
		}
	}
	closed = true
	if err := engine.Close(); err != nil {
		t.Fatal("own fixture engine close failed")
	}
	before, err := os.ReadFile(path)
	if err != nil || len(before) < 100 || before[18] != 1 || before[19] != 1 {
		t.Fatal("own fixture did not close in rollback journal format")
	}
	assertObservationSidecarsAbsent(t, path)
	return observationTestFixture{path: path, before: before, tokens: tokens}
}

func TestReadObservationInputs(t *testing.T) {
	args := []string{
		"--database", filepath.Join(t.TempDir(), "synthetic.sqlite"),
		"--project-id", "201",
		"--principal-id", "101",
		"--human-owner-id", "1",
		"--port", "53125",
	}
	options, err := parseReadObservationOptions(args)
	if err != nil || options.binding != observationBinding(observationTestProjectID) || options.port != 53125 || options.databasePath != args[1] {
		t.Fatal("canonical bounded options were refused or changed")
	}
	for _, candidate := range []struct {
		index int
		value string
	}{
		{index: 0, value: "--unknown"},
		{index: 1, value: "relative.sqlite"},
		{index: 1, value: args[1]+"\n"},
		{index: 2, value: "--database"},
		{index: 3, value: "0"},
		{index: 3, value: "-1"},
		{index: 3, value: "+201"},
		{index: 3, value: "0201"},
		{index: 3, value: "9223372036854775808"},
		{index: 5, value: "1"},
		{index: 7, value: "101"},
		{index: 9, value: "1023"},
		{index: 9, value: "65536"},
		{index: 9, value: "8765"},
		{index: 9, value: "053125"},
	} {
		changed := append([]string(nil), args...)
		changed[candidate.index] = candidate.value
		if _, err := parseReadObservationOptions(changed); err == nil {
			t.Fatal("noncanonical or unauthorized options were accepted")
		}
	}
	for _, candidate := range [][]string{nil, args[:8], append(append([]string(nil), args...), "extra")} {
		if _, err := parseReadObservationOptions(candidate); err == nil {
			t.Fatal("incorrect option count was accepted")
		}
	}
	for _, raw := range []string{"tk_12345678", "tk_"+strings.Repeat("x", 253)} {
		credential, err := readObservationCredential(strings.NewReader(raw+"\n"))
		if err != nil || credential != raw {
			t.Fatal("one bounded credential line was refused or changed")
		}
	}
	for _, input := range []string{
		"", "tk_12345678", "tk_12345678\r\n", "tk_short\n",
		"wrong_12345678\n", "tk_1234 5678\n", "tk_12345678\n\n",
		"tk_12345678\ntrailing", "tk_12345678\x00\n",
		"tk_"+strings.Repeat("x", 254)+"\n",
		"tk_"+strings.Repeat("x", 4096)+"\n",
	} {
		if credential, err := readObservationCredential(strings.NewReader(input)); err == nil || credential != "" {
			t.Fatal("unbounded, multiline or malformed credential was accepted")
		}
	}
	if credential, err := readObservationCredential(observationErrorReader{}); err == nil || credential != "" {
		t.Fatal("credential read failure was accepted")
	}
	reader := &observationCountingReader{next: strings.NewReader(strings.Repeat("x", 8192))}
	if credential, err := readObservationCredential(reader); err == nil || credential != "" || reader.bytesRead > 4097 {
		t.Fatal("credential input read exceeded its bounded limit")
	}
	for _, hash := range []string{"", "short", strings.Repeat("z", 64), strings.Repeat("A", 64), strings.Repeat("0", 65)} {
		if h, err := apiv2.NewReadObservationHandler(observationBinding(observationTestProjectID), hash); h != nil || err == nil {
			t.Fatal("invalid fixed credential hash was accepted by handler constructor")
		}
	}
	for _, binding := range []auth.ReadObservationBinding{
		{},
		{ProjectID: -1, PrincipalID: 101, HumanOwnerID: 1},
		{ProjectID: 201, PrincipalID: 0, HumanOwnerID: 1},
		{ProjectID: 201, PrincipalID: 101, HumanOwnerID: 101},
	} {
		if h, err := apiv2.NewReadObservationHandler(binding, models.HashAPIToken("tk_synthetic00")); h != nil || err == nil {
			t.Fatal("invalid explicit binding was accepted by handler constructor")
		}
	}
}

type observationErrorReader struct{}

func (observationErrorReader) Read([]byte) (int, error) {
	return 0, errors.New("synthetic-reader-failure")
}

type observationCountingReader struct {
	next io.Reader
	bytesRead int
}

func (r *observationCountingReader) Read(p []byte) (int, error) {
	n, err := r.next.Read(p)
	r.bytesRead += n
	return n, err
}

func TestReadObservationRequestLifecycle(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	finished := make(chan struct{})
	var dispatched atomic.Int32
	requests := &readObservationRequests{next: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		dispatched.Add(1)
		close(entered)
		<-release
		w.WriteHeader(http.StatusOK)
	})}
	go func() {
		requests.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/api/v2/user", nil))
		close(finished)
	}()
	// Every exit path releases and joins our one synthetic handler goroutine.
	defer func() {
		select {
		case <-release:
		default:
			close(release)
		}
		select {
		case <-finished:
		case <-time.After(time.Second):
			t.Error("synthetic request goroutine did not close")
		}
	}()
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("synthetic request did not enter handler")
	}
	requests.closeAdmission()
	refused := httptest.NewRecorder()
	requests.ServeHTTP(refused, httptest.NewRequest(http.MethodGet, "/api/v2/user", nil))
	if refused.Code != http.StatusServiceUnavailable || dispatched.Load() != 1 {
		t.Fatal("closed admission dispatched an additional handler")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if requests.wait(ctx) {
		t.Fatal("request join reported complete while native handler remained active")
	}
	close(release)
	select {
	case <-finished:
	case <-time.After(time.Second):
		t.Fatal("released synthetic handler did not finish")
	}
	joinCtx, cancelJoin := context.WithTimeout(context.Background(), time.Second)
	defer cancelJoin()
	if !requests.wait(joinCtx) {
		t.Fatal("finished request did not produce a bounded successful join")
	}
}

func withObservationEngine(t *testing.T, fixture observationTestFixture, run func(*xorm.Engine)) {
	t.Helper()
	engine, err := db.NewReadObservationEngine(context.Background(), fixture.path)
	if err != nil || engine == nil {
		t.Fatal("read-only fixture engine creation failed")
	}
	defer func() {
		if err := db.CloseEngine(engine); err != nil {
			t.Error("read-only fixture engine close failed")
		}
		after, err := os.ReadFile(fixture.path)
		if err != nil || !bytes.Equal(fixture.before, after) {
			t.Error("read-only fixture bytes changed")
		}
		assertObservationSidecarsAbsent(t, fixture.path)
	}()
	// db.CreateDBEngine returns the installed profile pointer before ordinary
	// configuration discovery, Ping, writable initialization or hooks.
	if err := models.SetEngine(); err != nil {
		t.Fatal("native models did not receive the installed engine")
	}
	if second, err := db.NewReadObservationEngine(context.Background(), fixture.path); err == nil || second != nil {
		t.Fatal("singleton engine ownership permitted a second installation")
	}
	if err := db.CloseEngine(nil); err == nil {
		t.Fatal("engine cleanup accepted a missing owned pointer")
	}
	run(engine)
}

func assertObservationSidecarsAbsent(t *testing.T, path string) {
	t.Helper()
	for _, suffix := range []string{"-wal", "-shm", "-journal"} {
		if _, err := os.Lstat(path+suffix); !errors.Is(err, os.ErrNotExist) {
			t.Error("fixture has an unexpected SQLite sidecar")
		}
	}
}

func observationDatabasePreflight(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "missing.sqlite")
	if engine, err := db.NewReadObservationEngine(context.Background(), missing); err == nil || engine != nil {
		if engine != nil {
			if closeErr := db.CloseEngine(engine); closeErr != nil {
				t.Error("unexpected missing database engine cleanup failed")
			}
		}
		t.Fatal("missing database was accepted")
	}
	if _, err := os.Lstat(missing); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("missing database preflight created a file")
	}
	fixture := observationFixture(t, nil, "")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if engine, err := db.NewReadObservationEngine(ctx, fixture.path); engine != nil || !errors.Is(err, context.Canceled) {
		t.Fatal("canceled database initialization was not refused")
	}
	if engine, err := db.NewReadObservationEngine(nil, fixture.path); engine != nil || err == nil {
		t.Fatal("nil database initialization context was accepted")
	}
	for _, path := range []string{
		"", "relative.sqlite", "memory", fixture.path+":synthetic",
		`\\localhost\synthetic\fixture.sqlite`, `\\?\C:\synthetic.sqlite`, `C:\NUL.sqlite`,
		strings.Repeat("x", 4097),
	} {
		if engine, err := db.NewReadObservationEngine(context.Background(), path); engine != nil || err == nil {
			t.Fatal("noncanonical database path was accepted")
		}
	}
	for _, suffix := range []string{"-wal", "-shm", "-journal"} {
		t.Run("existing"+suffix, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "sidecar.sqlite")
			if err := os.WriteFile(path, fixture.before, 0600); err != nil {
				t.Fatal("own sidecar fixture creation failed")
			}
			if err := os.WriteFile(path+suffix, []byte("synthetic-sidecar"), 0600); err != nil {
				t.Fatal("own synthetic sidecar creation failed")
			}
			if engine, err := db.NewReadObservationEngine(context.Background(), path); err == nil || engine != nil {
				if engine != nil {
					if closeErr := db.CloseEngine(engine); closeErr != nil {
						t.Error("unexpected sidecar database engine cleanup failed")
					}
				}
				t.Fatal("database with sidecar was accepted")
			}
			after, err := os.ReadFile(path)
			if err != nil || !bytes.Equal(fixture.before, after) {
				t.Fatal("sidecar refusal changed database bytes")
			}
			if data, err := os.ReadFile(path+suffix); err != nil || string(data) != "synthetic-sidecar" {
				t.Fatal("sidecar refusal changed or removed the sidecar")
			}
		})
	}
	t.Run("wal-header-without-sidecars", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "wal-header.sqlite")
		data := bytes.Clone(fixture.before)
		data[18], data[19] = 2, 2
		if err := os.WriteFile(path, data, 0600); err != nil {
			t.Fatal("own WAL-header fixture creation failed")
		}
		if engine, err := db.NewReadObservationEngine(context.Background(), path); err == nil || engine != nil {
			if engine != nil {
				if closeErr := db.CloseEngine(engine); closeErr != nil {
					t.Error("unexpected WAL-header engine cleanup failed")
				}
			}
			t.Fatal("WAL-format header was accepted without sidecars")
		}
		after, err := os.ReadFile(path)
		if err != nil || !bytes.Equal(data, after) {
			t.Fatal("WAL-header refusal changed fixture bytes")
		}
		assertObservationSidecarsAbsent(t, path)
	})
	t.Run("own-hardlinked-fixture", func(t *testing.T) {
		directory := t.TempDir()
		path := filepath.Join(directory, "hardlink-target.sqlite")
		alias := filepath.Join(directory, "hardlink-alias.sqlite")
		if err := os.WriteFile(path, fixture.before, 0600); err != nil {
			t.Fatal("own hardlink fixture creation failed")
		}
		if err := os.Link(path, alias); err != nil {
			t.Fatal("own hardlink fixture capability is unavailable; gate remains unverified")
		}
		defer func() {
			if err := os.Remove(alias); err != nil {
				t.Error("own hardlink alias cleanup failed")
			}
		}()
		for _, candidate := range []string{path, alias} {
			if engine, err := db.NewReadObservationEngine(context.Background(), candidate); err == nil || engine != nil {
				if engine != nil {
					if closeErr := db.CloseEngine(engine); closeErr != nil {
						t.Error("unexpected hardlinked engine cleanup failed")
					}
				}
				t.Fatal("hardlinked database was accepted")
			}
			after, err := os.ReadFile(candidate)
			if err != nil || !bytes.Equal(fixture.before, after) {
				t.Fatal("hardlink refusal changed database bytes")
			}
			assertObservationSidecarsAbsent(t, candidate)
		}
	})
}

func observationAuthentication(t *testing.T, fixture observationTestFixture) {
	ctx := context.Background()
	token, principal, err := auth.ValidateReadObservation(ctx, fixture.tokens["valid"], observationBinding(observationTestProjectID))
	if err != nil || token == nil || principal == nil || token.OwnerID != observationTestPrincipalID || principal.ID != observationTestPrincipalID || principal.BotOwnerID != observationTestHumanID {
		t.Fatal("valid modern token did not return actual bound native objects")
	}
	for _, candidate := range []struct {
		name string
		principalID int64
		humanID int64
	}{
		{name: "expired", principalID: 101, humanID: 1},
		{name: "disabled", principalID: 102, humanID: 1},
		{name: "locked", principalID: 103, humanID: 1},
		{name: "nonlocal", principalID: 104, humanID: 1},
		{name: "wrong-bot-owner", principalID: 105, humanID: 1},
		{name: "not-bot", principalID: 106, humanID: 1},
		{name: "disabled-human", principalID: 107, humanID: 4},
		{name: "nonlocal-human", principalID: 108, humanID: 5},
		{name: "bot-human", principalID: 109, humanID: 6},
		{name: "missing-principal", principalID: 99999, humanID: 1},
		{name: "legacy", principalID: 101, humanID: 1},
	} {
		t.Run(candidate.name, func(t *testing.T) {
			binding := observationBinding(observationTestProjectID)
			binding.PrincipalID, binding.HumanOwnerID = candidate.principalID, candidate.humanID
			if token, principal, err := auth.ValidateReadObservation(ctx, fixture.tokens[candidate.name], binding); err == nil || token != nil || principal != nil {
				t.Fatal("invalid credential or actual user binding was accepted")
			}
		})
	}
	for _, raw := range []string{"", "tk_short", "wrong-prefix-synthetic", "tk_"+strings.Repeat("x", 254)} {
		if token, err := models.GetReadObservationAPIToken(ctx, raw); err == nil || token != nil {
			t.Fatal("invalid token input was accepted")
		}
	}
	for _, binding := range []auth.ReadObservationBinding{
		{},
		{ProjectID: 201, PrincipalID: 101, HumanOwnerID: 101},
		{ProjectID: 201, PrincipalID: 102, HumanOwnerID: 1},
		{ProjectID: 201, PrincipalID: 101, HumanOwnerID: 2},
	} {
		if token, principal, err := auth.ValidateReadObservation(ctx, fixture.tokens["valid"], binding); err == nil || token != nil || principal != nil {
			t.Fatal("incorrect explicit binding was accepted")
		}
	}
}

func observationNewHandler(t *testing.T, fixture observationTestFixture, tokenName string, projectID int64) http.Handler {
	t.Helper()
	h, err := apiv2.NewReadObservationHandler(observationBinding(projectID), models.HashAPIToken(fixture.tokens[tokenName]))
	if err != nil || h == nil {
		t.Fatal("native read handler construction failed")
	}
	return h
}

func observationRequest(h http.Handler, raw, target string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(http.MethodGet, target, nil)
	r.Header.Set("Authorization", "Bearer "+raw)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func observationRefusal(t *testing.T, w *httptest.ResponseRecorder, status int) {
	t.Helper()
	if w.Code != status || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("unexpected refusal status or cache policy: %d", w.Code)
	}
	var body map[string]json.RawMessage
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil || len(body) != 1 {
		t.Fatal("refusal exposed a nonfixed response shape")
	}
	var code string
	if json.Unmarshal(body["code"], &code) != nil || code != "READ_OBSERVATION_REFUSED" {
		t.Fatal("refusal exposed a nonfixed response")
	}
}

func observationNativeHTTP(t *testing.T, fixture observationTestFixture) {
	h := observationNewHandler(t, fixture, "valid", observationTestProjectID)
	w := observationRequest(h, fixture.tokens["valid"], "/api/v2/user")
	var current struct {
		ID int64 `json:"id"`
		BotOwnerID int64 `json:"bot_owner_id"`
		IsLocalUser bool `json:"is_local_user"`
		AuthProvider string `json:"auth_provider"`
		Settings json.RawMessage `json:"settings"`
	}
	if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &current) != nil || current.ID != observationTestPrincipalID || current.BotOwnerID != observationTestHumanID || !current.IsLocalUser || current.AuthProvider != "local" || len(current.Settings) == 0 || string(current.Settings) == "null" {
		t.Fatal("genuine current-user DTO contract failed")
	}
	w = observationRequest(h, fixture.tokens["valid"], "/api/v2/projects/201")
	var project struct {
		ID int64 `json:"id"`
		Title string `json:"title"`
		MaxPermission *models.Permission `json:"max_permission"`
		Owner struct { ID int64 `json:"id"` } `json:"owner"`
	}
	if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &project) != nil || project.ID != observationTestProjectID || project.Title != "Synthetic accessible project" || project.Owner.ID != observationTestHumanID || project.MaxPermission == nil || *project.MaxPermission != models.PermissionRead {
		t.Fatal("genuine native project DTO or model ACL permission failed")
	}
	w = observationRequest(h, fixture.tokens["valid"], "/api/v2/projects/201/users?page=2&per_page=100")
	var page apiv2.Paginated[*models.UserWithPermission]
	if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &page) != nil || page.Page != 2 || page.PerPage != 100 || page.Total != observationTestShares || page.TotalPages != 2 || len(page.Items) != 21 {
		t.Fatal("genuine native user pagination contract failed")
	}
	for _, item := range page.Items {
		if item == nil || item.Email != "" || item.Permission != models.PermissionRead {
			t.Fatal("native shared-user privacy or permission contract failed")
		}
	}
	empty := observationNewHandler(t, fixture, "valid", observationTestProjectID)
	w = observationRequest(empty, fixture.tokens["valid"], "/api/v2/projects/201/users?page=50&per_page=100")
	if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &page) != nil || page.Page != 50 || page.Total != observationTestShares || page.Items == nil || len(page.Items) != 0 {
		t.Fatal("bounded empty native page did not retain the native array contract")
	}
	denied := observationNewHandler(t, fixture, "valid", observationTestDeniedProjectID)
	w = observationRequest(denied, fixture.tokens["valid"], "/api/v2/projects/202")
	if w.Code != http.StatusForbidden {
		t.Fatalf("native ACL did not refuse inaccessible project: %d", w.Code)
	}
	w = observationRequest(denied, fixture.tokens["valid"], "/api/v2/projects/202/users")
	if w.Code != http.StatusForbidden {
		t.Fatalf("native ACL did not refuse inaccessible members: %d", w.Code)
	}
	for _, tokenName := range []string{"no-scopes", "project-only"} {
		scoped := observationNewHandler(t, fixture, tokenName, observationTestProjectID)
		observationRefusal(t, observationRequest(scoped, fixture.tokens[tokenName], "/api/v2/user"), http.StatusUnauthorized)
		observationRefusal(t, observationRequest(scoped, fixture.tokens[tokenName], "/api/v2/projects/201/users"), http.StatusUnauthorized)
		if tokenName == "project-only" {
			if w := observationRequest(scoped, fixture.tokens[tokenName], "/api/v2/projects/201"); w.Code != http.StatusOK {
				t.Fatal("native project scope did not authorize its actual route")
			}
		} else {
			observationRefusal(t, observationRequest(scoped, fixture.tokens[tokenName], "/api/v2/projects/201"), http.StatusUnauthorized)
		}
	}
	quota := observationNewHandler(t, fixture, "valid", observationTestProjectID)
	emittedRecords := 0
	for n := 0; n < 50; n++ {
		w := observationRequest(quota, fixture.tokens["valid"], "/api/v2/projects/201/users?page=1&per_page=100")
		var bounded apiv2.Paginated[*models.UserWithPermission]
		if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &bounded) != nil || len(bounded.Items) != 100 {
			t.Fatalf("bounded successful page was refused before limit: %d", n)
		}
		emittedRecords += len(bounded.Items)
	}
	if emittedRecords != 5000 {
		t.Fatal("native quota test did not emit exactly its bounded record allowance")
	}
	observationRefusal(t, observationRequest(quota, fixture.tokens["valid"], "/api/v2/projects/201/users?page=1&per_page=100"), http.StatusServiceUnavailable)
}

func observationHTTPGates(t *testing.T, fixture observationTestFixture) {
	h := observationNewHandler(t, fixture, "valid", observationTestProjectID)
	// All of these are sent without credentials. A 400 proves the outer gate
	// wins over the authentication branch; it does not prove SQL call counts.
	for _, target := range []string{
		"/api/v2/projects/202", "/api/v2/projects/0201", "/api/v2/projects/+201",
		"/api/v2/projects/201/", "/api/v2/projects//201", "/api/v2/projects/201/../201",
		"/api/v2/projects/%32%30%31", "/api/v2/projects/201/users/",
		"/api/v2/openapi.json", "/api/v2/docs", "/api/v2/schemas/User.json",
		"/api/v2/user?x=1", "/api/v2/projects/201?page=1", "/api/v2/projects/201/users?q=synthetic",
		"/api/v2/projects/201/users?page=0", "/api/v2/projects/201/users?page=51",
		"/api/v2/projects/201/users?per_page=0", "/api/v2/projects/201/users?per_page=101",
		"/api/v2/projects/201/users?page=01", "/api/v2/projects/201/users?page=+1",
		"/api/v2/projects/201/users?page=%31", "/api/v2/projects/201/users?page=1&page=2",
		"/api/v2/projects/201/users?per_page=1&per_page=1", "/api/v2/projects/201/users?page=1&",
		"/api/v2/projects/201/users?page=1=1", "/api/v2/projects/201/users?Page=1",
		"/api/v2/projects/201/users?x="+strings.Repeat("x", 4096),
	} {
		r := httptest.NewRequest(http.MethodGet, target, nil)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		observationRefusal(t, w, http.StatusBadRequest)
	}
	for _, method := range []string{http.MethodHead, http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete, http.MethodOptions} {
		r := httptest.NewRequest(method, "/api/v2/user", nil)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		observationRefusal(t, w, http.StatusBadRequest)
	}
	for _, alter := range []func(*http.Request){
		func(r *http.Request) { r.Body = http.NoBody; r.ContentLength = 1 },
		func(r *http.Request) { r.Body = io.NopCloser(strings.NewReader("synthetic-body")); r.ContentLength = 0 },
		func(r *http.Request) { r.Body = http.NoBody; r.TransferEncoding = []string{"chunked"} },
		func(r *http.Request) { r.Body = http.NoBody; r.Trailer = http.Header{"Synthetic": {"trailer"}} },
		func(r *http.Request) { r.Header.Set("Expect", "100-continue") },
		func(r *http.Request) { r.Header.Set("Synthetic-Oversized", strings.Repeat("x", 8192)) },
		func(r *http.Request) { r.URL.Scheme, r.URL.Host = "http", "example.invalid" },
		func(r *http.Request) { r.URL.Fragment = "synthetic" },
		func(r *http.Request) { r.URL.ForceQuery = true },
		func(r *http.Request) { r.RequestURI = "/api/v2/user?mismatch=1" },
	} {
		r := httptest.NewRequest(http.MethodGet, "/api/v2/user", nil)
		alter(r)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		observationRefusal(t, w, http.StatusBadRequest)
	}
	for _, authorization := range []string{"", "Basic synthetic", "Bearer tk_short", "Bearer tk_"+strings.Repeat("x", 254), "Bearer "+fixture.tokens["expired"]} {
		r := httptest.NewRequest(http.MethodGet, "/api/v2/user", nil)
		r.Header.Set("Authorization", authorization)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		observationRefusal(t, w, http.StatusUnauthorized)
	}
	r := httptest.NewRequest(http.MethodGet, "/api/v2/user", nil)
	r.Header.Add("Authorization", "Bearer "+fixture.tokens["valid"])
	r.Header.Add("Authorization", "Bearer "+fixture.tokens["valid"])
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	observationRefusal(t, w, http.StatusUnauthorized)
	r = httptest.NewRequest(http.MethodGet, "/api/v2/user", nil)
	r.Header.Set("Authorization", "Bearer "+fixture.tokens["valid"])
	r.Header["authorization"] = []string{"Bearer "+fixture.tokens["valid"]}
	w = httptest.NewRecorder()
	h.ServeHTTP(w, r)
	observationRefusal(t, w, http.StatusUnauthorized)
	r = httptest.NewRequest(http.MethodGet, "/api/v2/user", nil)
	r.Header["aUtHoRiZaTiOn"] = []string{"Bearer "+fixture.tokens["valid"]}
	w = httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusOK {
		t.Fatal("single case-insensitive bearer header did not authenticate natively")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r = httptest.NewRequest(http.MethodGet, "/api/v2/user", nil).WithContext(ctx)
	r.Header.Set("Authorization", "Bearer "+fixture.tokens["valid"])
	w = httptest.NewRecorder()
	h.ServeHTTP(w, r)
	observationRefusal(t, w, http.StatusBadRequest)
}

type observationCancelProject struct {
	models.Project
	cancel context.CancelFunc
}

func (p *observationCancelProject) ReadOne(s *xorm.Session, a web.Auth) error {
	if err := p.Project.ReadOne(s, a); err != nil {
		return err
	}
	p.cancel()
	return nil
}

type observationCancelProjectUsers struct {
	models.ProjectUser
	cancel context.CancelFunc
}

func (p *observationCancelProjectUsers) ReadAll(s *xorm.Session, a web.Auth, search string, page, perPage int) (any, int, int64, error) {
	result, count, total, err := p.ProjectUser.ReadAll(s, a, search, page, perPage)
	if err == nil {
		p.cancel()
	}
	return result, count, total, err
}

func observationLateCancellation(t *testing.T) {
	a := &user.User{ID: observationTestPrincipalID, BotOwnerID: observationTestHumanID, Issuer: user.IssuerLocal, Status: user.StatusActive}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	project := &observationCancelProject{Project: models.Project{ID: observationTestProjectID}, cancel: cancel}
	if permission, err := handler.DoReadOne(ctx, project, a); permission != 0 || !errors.Is(err, context.Canceled) {
		t.Fatal("late cancellation after native project read returned permission")
	}
	ctxAll, cancelAll := context.WithCancel(context.Background())
	defer cancelAll()
	shares := &observationCancelProjectUsers{ProjectUser: models.ProjectUser{ProjectID: observationTestProjectID}, cancel: cancelAll}
	if result, count, total, err := handler.DoReadAll(ctxAll, shares, a, "", 1, 100); result != nil || count != 0 || total != 0 || !errors.Is(err, context.Canceled) {
		t.Fatal("late cancellation after native members read returned results")
	}
}

func observationResponseCaps(t *testing.T) {
	for _, candidate := range []struct {
		name string
		descriptionBytes int
		oversized bool
	}{
		{name: "single-response", descriptionBytes: 1024*1024+128, oversized: true},
		{name: "aggregate-responses", descriptionBytes: 512*1024},
	} {
		t.Run(candidate.name, func(t *testing.T) {
			fixture := observationFixture(t, nil, strings.Repeat("x", candidate.descriptionBytes))
			withObservationEngine(t, fixture, func(*xorm.Engine) {
				h := observationNewHandler(t, fixture, "valid", observationTestProjectID)
				if candidate.oversized {
					observationRefusal(t, observationRequest(h, fixture.tokens["valid"], "/api/v2/projects/201"), http.StatusInternalServerError)
					return
				}
				totalBytes := 0
				refused := false
				for n := 0; n < 20; n++ {
					w := observationRequest(h, fixture.tokens["valid"], "/api/v2/projects/201")
					if w.Code == http.StatusInternalServerError || w.Code == http.StatusServiceUnavailable {
						observationRefusal(t, w, w.Code)
						refused = true
						break
					}
					if w.Code != http.StatusOK || w.Body.Len() > 1024*1024 {
						t.Fatal("native aggregate cap failed before a bounded successful response")
					}
					totalBytes += w.Body.Len()
					if totalBytes > 8*1024*1024 {
						t.Fatal("native handler emitted more than its aggregate response budget")
					}
				}
				if !refused || totalBytes == 0 {
					t.Fatal("native aggregate response budget was not exercised")
				}
			})
		})
	}
}
