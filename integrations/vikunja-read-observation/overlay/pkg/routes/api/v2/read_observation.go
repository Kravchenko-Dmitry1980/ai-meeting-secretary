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

package apiv2

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"code.vikunja.io/api/pkg/log"
	"code.vikunja.io/api/pkg/models"
	"code.vikunja.io/api/pkg/modules/auth"

	"github.com/danielgtaylor/huma/v2"
	"github.com/labstack/echo/v5"
)

const (
	observationHeaderLimit = 8 * 1024
	observationTargetLimit = 4 * 1024
	observationResponseLimit = 1024 * 1024
	observationTotalResponseLimit = 8 * 1024 * 1024
	observationPageLimit = 50
	observationRecordLimit = 5000
	observationRequestLimit = 2 * time.Second
)

var errObservationResponseLimit = errors.New("read observation response refused")

type readObservationHandler struct {
	echo *echo.Echo
	projectPath string
	usersPath string
	admission chan struct{}
	pages int
	records int
	responseBytes int
}

// NewReadObservationHandler exposes genuine native reads without enrolling this
// profile in RegisterAll or giving the normal server a new public registrar.
func NewReadObservationHandler(binding auth.ReadObservationBinding, expectedTokenSHA256 string) (http.Handler, error) {
	if binding.ProjectID <= 0 || binding.PrincipalID <= 0 || binding.HumanOwnerID <= 0 ||
		binding.PrincipalID == binding.HumanOwnerID || len(expectedTokenSHA256) != 64 ||
		strings.ToLower(expectedTokenSHA256) != expectedTokenSHA256 {
		return nil, errors.New("read observation binding refused")
	}
	expectedHash, err := hex.DecodeString(expectedTokenSHA256)
	if err != nil || len(expectedHash) != sha256.Size {
		return nil, errors.New("read observation binding refused")
	}
	e := echo.New()
	e.Logger = log.NewEchoLogger(false, "off", "ERROR", "text")
	e.HTTPErrorHandler = func(c *echo.Context, _ error) {
		_ = c.JSON(http.StatusInternalServerError, map[string]string{
			"code": "READ_OBSERVATION_REFUSED",
		})
	}
	g := e.Group(GroupPrefix)
	g.Use(func(next echo.HandlerFunc) echo.HandlerFunc {
		return func(c *echo.Context) error {
			header, present := observationAuthorization(c.Request().Header)
			if !present || !strings.HasPrefix(header, "Bearer tk_") || len(header) > 263 {
				return observationEchoRefusal(c, http.StatusUnauthorized)
			}
			raw := strings.TrimPrefix(header, "Bearer ")
			if !observationCredential(raw) {
				return observationEchoRefusal(c, http.StatusUnauthorized)
			}
			actualHash := sha256.Sum256([]byte(raw))
			if subtle.ConstantTimeCompare(actualHash[:], expectedHash) != 1 {
				return observationEchoRefusal(c, http.StatusUnauthorized)
			}
			token, principal, err := auth.ValidateReadObservation(c.Request().Context(), raw, binding)
			if err != nil || token == nil || principal == nil || c.Request().Context().Err() != nil {
				return observationEchoRefusal(c, http.StatusUnauthorized)
			}
			if !models.CanDoAPIRoute(c, token) {
				return observationEchoRefusal(c, http.StatusUnauthorized)
			}
			c.Set("api_token", token)
			c.Set("api_user", principal)
			return next(c)
		}
	})
	api := NewAPI(e, g)
	Register(api, huma.Operation{
		OperationID: "user-show",
		Summary: "Get the current user",
		Description: "Returns the actual authenticated user and native account settings.",
		Method: http.MethodGet,
		Path: "/user",
		Tags: []string{"user"},
	}, userShow)
	Register(api, huma.Operation{
		OperationID: "projects-read",
		Summary: "Get a project",
		Description: "Returns the native project, derived state and caller permission after model ACL checks.",
		Method: http.MethodGet,
		Path: "/projects/{id}",
		Tags: []string{"projects"},
	}, projectsRead)
	Register(api, huma.Operation{
		OperationID: "project-users-list",
		Summary: "List the users a project is shared with",
		Description: "Returns native paginated direct user shares after the project's model ACL checks; team shares are excluded.",
		Method: http.MethodGet,
		Path: "/projects/{project}/users",
		Tags: []string{"sharing"},
	}, projectUsersList)
	for _, route := range e.Router().Routes() {
		if route.Method == http.MethodGet && (route.Path == GroupPrefix+"/user" ||
			route.Path == GroupPrefix+"/projects/:id" || route.Path == GroupPrefix+"/projects/:project/users") {
			models.CollectRoutesForAPITokenUsage(route, true)
		}
	}
	projectPath := GroupPrefix + "/projects/" + strconv.FormatInt(binding.ProjectID, 10)
	return &readObservationHandler{
		echo: e,
		projectPath: projectPath,
		usersPath: projectPath + "/users",
		admission: make(chan struct{}, 1),
	}, nil
}

func observationEchoRefusal(c *echo.Context, status int) error {
	return c.JSON(status, map[string]string{
		"code": "READ_OBSERVATION_REFUSED",
	})
}

func observationCredential(raw string) bool {
	if len(raw) < len("tk_")+8 || len(raw) > 256 || !strings.HasPrefix(raw, "tk_") {
		return false
	}
	for _, b := range []byte(raw) {
		if b <= 32 || b >= 127 {
			return false
		}
	}
	return true
}

func observationAuthorization(header http.Header) (string, bool) {
	value := ""
	found := false
	for key, values := range header {
		if !strings.EqualFold(key, "Authorization") {
			continue
		}
		if found || len(values) != 1 {
			return "", false
		}
		value = values[0]
		found = true
	}
	return value, found
}

func (h *readObservationHandler) canonical(r *http.Request) bool {
	if r == nil || r.URL == nil || r.Method != http.MethodGet || r.URL.Scheme != "" ||
		r.URL.Host != "" || r.URL.Opaque != "" || r.URL.RawPath != "" || r.URL.Fragment != "" ||
		r.URL.RawFragment != "" || r.URL.ForceQuery || r.ContentLength != 0 ||
		len(r.TransferEncoding) != 0 || len(r.Trailer) != 0 || r.Header.Get("Expect") != "" ||
		(r.Body != nil && r.Body != http.NoBody) {
		return false
	}
	target := r.URL.EscapedPath()
	if target != r.URL.Path {
		return false
	}
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}
	if len(target) > observationTargetLimit || (r.RequestURI != "" && r.RequestURI != target) {
		return false
	}
	headerBytes := len(r.Host) + 8
	if headerBytes > observationHeaderLimit {
		return false
	}
	for key, values := range r.Header {
		for _, value := range values {
			headerBytes += len(key) + len(value) + 4
			if headerBytes > observationHeaderLimit {
				return false
			}
		}
	}
	if r.URL.Path == GroupPrefix+"/user" || r.URL.Path == h.projectPath {
		return r.URL.RawQuery == ""
	}
	if r.URL.Path != h.usersPath {
		return false
	}
	if r.URL.RawQuery == "" {
		return true
	}
	seen := map[string]bool{}
	for _, pair := range strings.Split(r.URL.RawQuery, "&") {
		parts := strings.Split(pair, "=")
		if len(parts) != 2 || seen[parts[0]] {
			return false
		}
		limit := 0
		switch parts[0] {
		case "page":
			limit = observationPageLimit
		case "per_page":
			limit = 100
		default:
			return false
		}
		value, err := strconv.Atoi(parts[1])
		if err != nil || value < 1 || value > limit || strconv.Itoa(value) != parts[1] {
			return false
		}
		seen[parts[0]] = true
	}
	return true
}

func (h *readObservationHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if !h.canonical(r) || r.Context().Err() != nil {
		observationHTTPRefusal(w, http.StatusBadRequest)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), observationRequestLimit)
	defer cancel()
	r = r.WithContext(ctx)
	// Waiting for singleton admission must also respect the request deadline.
	select {
	case h.admission <- struct{}{}:
		defer func() { <-h.admission }()
	case <-ctx.Done():
		observationHTTPRefusal(w, http.StatusGatewayTimeout)
		return
	}
	users := r.URL.Path == h.usersPath
	if ctx.Err() != nil || (users && h.pages >= observationPageLimit) ||
		h.responseBytes >= observationTotalResponseLimit {
		observationHTTPRefusal(w, http.StatusServiceUnavailable)
		return
	}
	buffer := &observationResponseBuffer{
		header: make(http.Header),
		body: make([]byte, 0, observationResponseLimit),
	}
	h.echo.ServeHTTP(buffer, r)
	if ctx.Err() != nil {
		observationHTTPRefusal(w, http.StatusGatewayTimeout)
		return
	}
	if buffer.overflow || buffer.status >= 500 || buffer.status < 200 ||
		len(buffer.body) > observationTotalResponseLimit-h.responseBytes {
		observationHTTPRefusal(w, http.StatusInternalServerError)
		return
	}
	if users && buffer.status == http.StatusOK {
		var page struct {
			Items []json.RawMessage `json:"items"`
		}
		if err := json.Unmarshal(buffer.body, &page); err != nil || len(page.Items) > 100 ||
			len(page.Items) > observationRecordLimit-h.records {
			observationHTTPRefusal(w, http.StatusInternalServerError)
			return
		}
		h.pages++
		h.records += len(page.Items)
	}
	h.responseBytes += len(buffer.body)
	for key, values := range buffer.header {
		for _, value := range values {
			w.Header().Add(key, value)
		}
	}
	w.Header().Set("Cache-Control", "no-store")
	if ctx.Err() != nil {
		observationHTTPRefusal(w, http.StatusGatewayTimeout)
		return
	}
	w.WriteHeader(buffer.status)
	_, _ = w.Write(buffer.body)
}

func observationHTTPRefusal(w http.ResponseWriter, status int) {
	for key := range w.Header() {
		w.Header().Del(key)
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_, _ = w.Write([]byte("{\"code\":\"READ_OBSERVATION_REFUSED\"}\n"))
}

type observationResponseBuffer struct {
	header http.Header
	status int
	body []byte
	overflow bool
}

func (b *observationResponseBuffer) Header() http.Header { return b.header }

func (b *observationResponseBuffer) WriteHeader(status int) {
	if b.status != 0 {
		return
	}
	if status < 200 || status > 599 {
		b.overflow = true
		return
	}
	b.status = status
}

func (b *observationResponseBuffer) Write(p []byte) (int, error) {
	if b.status == 0 {
		b.WriteHeader(http.StatusOK)
	}
	if b.overflow || len(p) > observationResponseLimit-len(b.body) {
		b.overflow = true
		return 0, errObservationResponseLimit
	}
	b.body = append(b.body, p...)
	return len(p), nil
}
