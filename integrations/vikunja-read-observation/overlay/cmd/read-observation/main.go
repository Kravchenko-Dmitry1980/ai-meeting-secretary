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
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	standardlog "log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"code.vikunja.io/api/pkg/config"
	"code.vikunja.io/api/pkg/db"
	"code.vikunja.io/api/pkg/license"
	"code.vikunja.io/api/pkg/models"
	"code.vikunja.io/api/pkg/modules/auth"
	"code.vikunja.io/api/pkg/modules/keyvalue"
	apiv2 "code.vikunja.io/api/pkg/routes/api/v2"
)

const (
	readObservationStdinLimit = 4 * 1024
	readObservationStartupLimit = 15 * time.Second
	readObservationProcessLimit = 60 * time.Second
	readObservationShutdownLimit = 5 * time.Second
)

var errReadObservationRefused = errors.New("read observation refused")

type readObservationOptions struct {
	databasePath string
	binding auth.ReadObservationBinding
	port int
}

func main() {
	// These hard limits cover blocked stdin/CGO/Close as well as cooperative contexts.
	processWatchdog := time.AfterFunc(readObservationProcessLimit, func() { os.Exit(70) })
	startupWatchdog := time.AfterFunc(readObservationStartupLimit, func() { os.Exit(70) })
	ctx, cancel := context.WithTimeout(context.Background(), readObservationProcessLimit-readObservationShutdownLimit)
	defer cancel()
	options, err := parseReadObservationOptions(os.Args[1:])
	if err == nil {
		var credential string
		credential, err = readObservationCredential(os.Stdin)
		if err == nil {
			err = runReadObservation(ctx, options, credential, os.Stdout, startupWatchdog)
		}
	}
	startupWatchdog.Stop()
	if err != nil {
		_, _ = io.WriteString(os.Stderr, "READ_OBSERVATION_REFUSED\n")
		os.Exit(70)
	}
	if _, err = io.WriteString(os.Stdout, "READ_OBSERVATION_STOPPED\n"); err != nil {
		os.Exit(70)
	}
	processWatchdog.Stop()
}

func parseReadObservationOptions(args []string) (readObservationOptions, error) {
	var options readObservationOptions
	if len(args) != 10 {
		return options, errReadObservationRefused
	}
	values := make(map[string]string, 5)
	for i := 0; i < len(args); i += 2 {
		key := args[i]
		switch key {
		case "--database", "--project-id", "--principal-id", "--human-owner-id", "--port":
		default:
			return options, errReadObservationRefused
		}
		if _, duplicate := values[key]; duplicate || len(args[i+1]) == 0 || len(args[i+1]) > 2048 {
			return options, errReadObservationRefused
		}
		values[key] = args[i+1]
	}
	options.databasePath = values["--database"]
	if !filepath.IsAbs(options.databasePath) || strings.ContainsAny(options.databasePath, "\x00\r\n") {
		return readObservationOptions{}, errReadObservationRefused
	}
	project, err := readObservationPositiveInt(values["--project-id"])
	if err != nil {
		return readObservationOptions{}, errReadObservationRefused
	}
	principal, err := readObservationPositiveInt(values["--principal-id"])
	if err != nil {
		return readObservationOptions{}, errReadObservationRefused
	}
	owner, err := readObservationPositiveInt(values["--human-owner-id"])
	if err != nil || owner == principal {
		return readObservationOptions{}, errReadObservationRefused
	}
	port, err := readObservationPositiveInt(values["--port"])
	if err != nil || port < 1024 || port > 65535 || port == 8765 {
		return readObservationOptions{}, errReadObservationRefused
	}
	options.binding = auth.ReadObservationBinding{
		ProjectID: project,
		PrincipalID: principal,
		HumanOwnerID: owner,
	}
	options.port = int(port)
	return options, nil
}

func readObservationPositiveInt(raw string) (int64, error) {
	value, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || value <= 0 || strconv.FormatInt(value, 10) != raw {
		return 0, errReadObservationRefused
	}
	return value, nil
}

func readObservationCredential(reader io.Reader) (string, error) {
	input, err := io.ReadAll(io.LimitReader(reader, readObservationStdinLimit+1))
	if err != nil || len(input) > readObservationStdinLimit || len(input) < 12 ||
		input[len(input)-1] != '\n' {
		return "", errReadObservationRefused
	}
	credential := string(input[:len(input)-1])
	if len(credential) > 256 || len(credential) < len("tk_")+8 || !strings.HasPrefix(credential, "tk_") {
		return "", errReadObservationRefused
	}
	for _, b := range []byte(credential) {
		if b <= 32 || b >= 127 {
			return "", errReadObservationRefused
		}
	}
	return credential, nil
}

func runReadObservation(
	ctx context.Context,
	options readObservationOptions,
	credential string,
	ready io.Writer,
	startupWatchdog *time.Timer,
) (result error) {
	startupCtx, cancelStartup := context.WithTimeout(ctx, readObservationStartupLimit)
	defer cancelStartup()
	if ctx.Err() != nil || config.InitReadObservation() != nil {
		return errReadObservationRefused
	}
	// The fresh command performs exactly one fixed memory-backend initialization.
	keyvalue.InitStorage()
	engine, err := db.NewReadObservationEngine(startupCtx, options.databasePath)
	if err != nil || engine == nil {
		return errReadObservationRefused
	}
	defer func() {
		if db.CloseEngine(engine) != nil {
			result = errReadObservationRefused
		}
	}()
	// SetEngine reuses the installed db singleton; it must not initialize a second engine.
	if models.SetEngine() != nil {
		return errReadObservationRefused
	}
	if _, _, err = auth.ValidateReadObservation(startupCtx, credential, options.binding); err != nil {
		return errReadObservationRefused
	}
	if license.InitReadObservation(startupCtx) != nil || startupCtx.Err() != nil {
		return errReadObservationRefused
	}
	tokenHash := sha256.Sum256([]byte(credential))
	handler, err := apiv2.NewReadObservationHandler(options.binding, hex.EncodeToString(tokenHash[:]))
	if err != nil || startupCtx.Err() != nil {
		return errReadObservationRefused
	}
	listener, err := net.Listen("tcp4", "127.0.0.1:"+strconv.Itoa(options.port))
	if err != nil {
		return errReadObservationRefused
	}
	requests := &readObservationRequests{next: handler}
	server := &http.Server{
		Handler: requests,
		ReadHeaderTimeout: 3 * time.Second,
		ReadTimeout: 3 * time.Second,
		WriteTimeout: 2 * time.Second,
		IdleTimeout: 2 * time.Second,
		MaxHeaderBytes: 8 * 1024,
		ErrorLog: standardlog.New(io.Discard, "", 0),
		BaseContext: func(net.Listener) context.Context { return ctx },
	}
	server.SetKeepAlivesEnabled(false)
	if startupCtx.Err() != nil || !startupWatchdog.Stop() {
		if listener.Close() != nil {
			return errReadObservationRefused
		}
		return errReadObservationRefused
	}
	cancelStartup()
	serveResult := make(chan error, 1)
	go func() { serveResult <- server.Serve(listener) }()
	if _, err = io.WriteString(ready, "READ_OBSERVATION_READY\n"); err != nil {
		result = errReadObservationRefused
	}
	serveReceived := false
	if result == nil {
		select {
		case err = <-serveResult:
			serveReceived = true
			if !errors.Is(err, http.ErrServerClosed) {
				result = errReadObservationRefused
			}
		case <-ctx.Done():
		}
	}
	shutdownCtx, cancelShutdown := context.WithTimeout(context.Background(), readObservationShutdownLimit)
	defer cancelShutdown()
	requests.closeAdmission()
	if server.Shutdown(shutdownCtx) != nil {
		result = errReadObservationRefused
		if server.Close() != nil {
			result = errReadObservationRefused
		}
	}
	if !requests.wait(shutdownCtx) {
		// Never close the engine underneath unjoined native SQL. The launcher
		// retains the original NoWrite guard until this process actually exits.
		os.Exit(70)
	}
	if !serveReceived {
		select {
		case err = <-serveResult:
			if !errors.Is(err, http.ErrServerClosed) {
				result = errReadObservationRefused
			}
		case <-shutdownCtx.Done():
			result = errReadObservationRefused
		}
	}
	return result
}

type readObservationRequests struct {
	next http.Handler
	mu sync.Mutex
	active sync.WaitGroup
	closed bool
}

func (r *readObservationRequests) ServeHTTP(w http.ResponseWriter, request *http.Request) {
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte("{\"code\":\"READ_OBSERVATION_REFUSED\"}\n"))
		return
	}
	r.active.Add(1)
	r.mu.Unlock()
	defer r.active.Done()
	r.next.ServeHTTP(w, request)
}

func (r *readObservationRequests) closeAdmission() {
	r.mu.Lock()
	r.closed = true
	r.mu.Unlock()
}

func (r *readObservationRequests) wait(ctx context.Context) bool {
	done := make(chan struct{})
	go func() {
		r.active.Wait()
		close(done)
	}()
	select {
	case <-done:
		return true
	case <-ctx.Done():
		return false
	}
}
