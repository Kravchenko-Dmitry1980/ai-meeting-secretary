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
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU Affero General Public License for more details.
//
// You should have received a copy of the GNU Affero General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.

package db

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"code.vikunja.io/api/pkg/config"
	"code.vikunja.io/api/pkg/log"

	"xorm.io/xorm"
	"xorm.io/xorm/names"
)

var readObservationOwnedEngine *xorm.Engine

// NewReadObservationEngine requires single-threaded startup and externally held NoWrite custody.
func NewReadObservationEngine(ctx context.Context, path string) (_ *xorm.Engine, err error) {
	if err = readObservationContext(ctx); err != nil {
		return nil, err
	}
	if x != nil || readObservationOwnedEngine != nil {
		return nil, errors.New("read observation database already initialized")
	}
	if config.DatabaseType.GetString() != "sqlite" || config.ServiceTimeZone.GetString() != "GMT" {
		return nil, errors.New("read observation database configuration invalid")
	}
	canonicalPath, err := readObservationPath(path)
	if err != nil {
		return nil, err
	}
	before, err := readObservationPreflight(ctx, canonicalPath)
	if err != nil {
		return nil, err
	}

	query := url.Values{
		"mode":          {"ro"},
		"_query_only":   {"on"},
		"cache":         {"private"},
		"_busy_timeout": {"1000"},
	}
	uri := url.URL{
		Scheme:   "file",
		Path:     "/" + filepath.ToSlash(canonicalPath),
		RawQuery: query.Encode(),
	}
	engine, openErr := xorm.NewEngine("sqlite3", uri.String())
	installed := false
	if engine != nil {
		defer func() {
			if !installed {
				if closeErr := engine.Close(); closeErr != nil {
					err = errors.Join(err, fmt.Errorf("close uninstalled read observation engine: %w", closeErr))
				}
			}
		}()
	}
	if openErr != nil {
		return nil, fmt.Errorf("open read observation engine: %w", openErr)
	}
	if engine == nil {
		return nil, errors.New("read observation engine unavailable")
	}
	engine.SetMaxOpenConns(1)
	engine.SetMaxIdleConns(1)
	engine.SetConnMaxLifetime(0)
	engine.SetMapper(names.GonicMapper{})
	engine.SetTZLocation(config.GetTimeZone())
	engine.SetTZDatabase(time.FixedZone("GMT", 0))
	logger := log.NewXormLogger(false, "off", "ERROR", "text")
	logger.ShowSQL(false)
	engine.SetLogger(logger)
	engine.AddHook(writeInvalidationHook{})
	if err = readObservationContext(ctx); err != nil {
		return nil, err
	}
	if engine.DB() == nil || engine.DB().DB == nil {
		return nil, errors.New("read observation database connection unavailable")
	}
	if err = engine.DB().DB.PingContext(ctx); err != nil {
		return nil, fmt.Errorf("ping read observation database: %w", err)
	}
	after, err := readObservationPreflight(ctx, canonicalPath)
	if err != nil {
		return nil, err
	}
	if !readObservationSameFile(before, after) {
		return nil, errors.New("read observation database changed during opening")
	}
	if err = readObservationContext(ctx); err != nil {
		return nil, err
	}
	x = engine
	readObservationOwnedEngine = engine
	installed = true
	return engine, nil
}

func NewReadObservationReadSession(ctx context.Context) (*xorm.Session, error) {
	if err := readObservationContext(ctx); err != nil {
		return nil, err
	}
	if x == nil {
		return nil, errors.New("read observation database not initialized")
	}
	s := x.NewSession()
	attachSessionCache(s)
	SetSessionContext(ctx, s)
	if err := readObservationContext(ctx); err != nil {
		return nil, readObservationCloseSession(s, err)
	}
	return s, nil
}

func NewReadObservationTransaction(ctx context.Context) (*xorm.Session, error) {
	s, err := NewReadObservationReadSession(ctx)
	if err != nil {
		return nil, err
	}
	if err = readObservationContext(ctx); err != nil {
		return nil, readObservationCloseSession(s, err)
	}
	if err = s.Begin(); err != nil {
		return nil, readObservationCloseSession(s, fmt.Errorf("begin read observation transaction: %w", err))
	}
	if err = readObservationContext(ctx); err != nil {
		return nil, readObservationCloseSession(s, err)
	}
	return s, nil
}

// CloseEngine is called only after all request handlers have stopped using the engine.
func CloseEngine(engine *xorm.Engine) error {
	if engine == nil || engine != readObservationOwnedEngine || x != engine {
		return errors.New("read observation engine ownership mismatch")
	}
	err := engine.Close()
	// A failed close must never leave an engine available for further requests.
	x = nil
	readObservationOwnedEngine = nil
	if err != nil {
		return fmt.Errorf("close read observation engine: %w", err)
	}
	return nil
}

func readObservationCloseSession(s *xorm.Session, err error) error {
	if closeErr := s.Close(); closeErr != nil {
		return errors.Join(err, fmt.Errorf("close read observation session: %w", closeErr))
	}
	return err
}

func readObservationContext(ctx context.Context) error {
	if ctx == nil {
		return errors.New("read observation context missing")
	}
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("read observation context stopped: %w", err)
	}
	return nil
}

func readObservationPath(path string) (string, error) {
	if path == "" || len(path) > 4096 {
		return "", errors.New("read observation database path invalid")
	}
	native := filepath.FromSlash(path)
	volume := filepath.VolumeName(native)
	if len(volume) != 2 || volume[1] != ':' ||
		!((volume[0] >= 'A' && volume[0] <= 'Z') || (volume[0] >= 'a' && volume[0] <= 'z')) ||
		!filepath.IsAbs(native) || filepath.Clean(native) != native {
		return "", errors.New("read observation database path must be canonical and drive-local")
	}
	components := strings.Split(native[len(volume)+1:], string(filepath.Separator))
	for _, component := range components {
		if component == "" || strings.HasSuffix(component, ".") || strings.HasSuffix(component, " ") ||
			strings.ContainsAny(component, ":<>\"|?*") {
			return "", errors.New("read observation database path component invalid")
		}
		for _, character := range component {
			if character < 32 || character == 127 {
				return "", errors.New("read observation database path control character")
			}
		}
		base := strings.ToUpper(strings.SplitN(component, ".", 2)[0])
		if base == "CON" || base == "PRN" || base == "AUX" || base == "NUL" ||
			base == "CLOCK$" || base == "CONIN$" || base == "CONOUT$" {
			return "", errors.New("read observation database device path refused")
		}
		if strings.HasPrefix(base, "COM") || strings.HasPrefix(base, "LPT") {
			suffix := base[3:]
			if strings.Contains("123456789¹²³", suffix) && suffix != "" && len([]rune(suffix)) == 1 {
				return "", errors.New("read observation database device path refused")
			}
		}
	}
	return native, nil
}

// These bounded checks do not replace the launcher's continuous file-identity and NoWrite custody.
func readObservationPreflight(ctx context.Context, path string) (_ os.FileInfo, err error) {
	before, err := readObservationPathInfo(ctx, path)
	if err != nil {
		return nil, err
	}
	if err = readObservationNoSidecars(path); err != nil {
		return nil, err
	}
	if err = readObservationContext(ctx); err != nil {
		return nil, err
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("open read observation database header: %w", err)
	}
	defer func() {
		if closeErr := file.Close(); closeErr != nil {
			err = errors.Join(err, fmt.Errorf("close read observation database header: %w", closeErr))
		}
		if ctxErr := readObservationContext(ctx); ctxErr != nil {
			err = errors.Join(err, ctxErr)
		}
	}()
	opened, err := file.Stat()
	if err != nil {
		return nil, fmt.Errorf("inspect opened read observation database: %w", err)
	}
	if !readObservationSameFile(before, opened) {
		return nil, errors.New("read observation database identity changed")
	}
	var handleInfo syscall.ByHandleFileInformation
	if err = syscall.GetFileInformationByHandle(syscall.Handle(file.Fd()), &handleInfo); err != nil {
		return nil, fmt.Errorf("inspect read observation database handle: %w", err)
	}
	if handleInfo.NumberOfLinks != 1 || handleInfo.FileAttributes&syscall.FILE_ATTRIBUTE_REPARSE_POINT != 0 {
		return nil, errors.New("read observation database links or handle attributes refused")
	}
	var header [100]byte
	if _, err = io.ReadFull(file, header[:]); err != nil {
		return nil, fmt.Errorf("read read observation database header: %w", err)
	}
	if string(header[:16]) != "SQLite format 3\x00" || header[18] != 1 || header[19] != 1 {
		return nil, errors.New("read observation database requires a rollback-mode SQLite header")
	}
	after, err := readObservationPathInfo(ctx, path)
	if err != nil {
		return nil, err
	}
	if !readObservationSameFile(before, after) {
		return nil, errors.New("read observation database changed during header inspection")
	}
	if err = readObservationNoSidecars(path); err != nil {
		return nil, err
	}
	return after, nil
}

func readObservationPathInfo(ctx context.Context, path string) (os.FileInfo, error) {
	volume := filepath.VolumeName(path)
	current := volume + string(filepath.Separator)
	components := strings.Split(path[len(current):], string(filepath.Separator))
	for index := -1; index < len(components); index++ {
		if err := readObservationContext(ctx); err != nil {
			return nil, err
		}
		if index >= 0 {
			current = filepath.Join(current, components[index])
		}
		info, err := os.Lstat(current)
		if err != nil {
			return nil, fmt.Errorf("inspect read observation database path: %w", err)
		}
		attributes, ok := info.Sys().(*syscall.Win32FileAttributeData)
		if !ok || attributes.FileAttributes&syscall.FILE_ATTRIBUTE_REPARSE_POINT != 0 ||
			info.Mode()&os.ModeSymlink != 0 {
			return nil, errors.New("read observation database path attributes refused")
		}
		if index == len(components)-1 {
			if !info.Mode().IsRegular() || info.Size() < 100 {
				return nil, errors.New("read observation database must be an existing regular SQLite file")
			}
			return info, nil
		}
		if !info.IsDir() {
			return nil, errors.New("read observation database parent is not a directory")
		}
	}
	return nil, errors.New("read observation database path incomplete")
}

func readObservationNoSidecars(path string) error {
	for _, suffix := range []string{
		"-journal",
		"-wal",
		"-shm",
	} {
		_, err := os.Lstat(path + suffix)
		if err == nil {
			return errors.New("read observation database sidecar present")
		}
		if !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("inspect read observation database sidecar: %w", err)
		}
	}
	return nil
}

func readObservationSameFile(before, after os.FileInfo) bool {
	return os.SameFile(before, after) && before.Mode() == after.Mode() &&
		before.Size() == after.Size() && before.ModTime().Equal(after.ModTime())
}
