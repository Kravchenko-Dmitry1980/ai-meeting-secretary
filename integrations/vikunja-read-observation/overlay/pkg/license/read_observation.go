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

package license

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"code.vikunja.io/api/pkg/config"
	"code.vikunja.io/api/pkg/db"
	"code.vikunja.io/api/pkg/modules/keyvalue"
)

// InitReadObservation requires the command's fresh, single-threaded memory KV startup.
func InitReadObservation(ctx context.Context) error {
	if ctx == nil {
		return fmt.Errorf("read observation license context is required")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if config.LicenseKey.GetString() != "" || config.KeyvalueType.GetString() != "memory" || config.RedisEnabled.GetBool() {
		return fmt.Errorf("read observation requires the unlicensed memory configuration")
	}
	if instanceID != "" || stopCh != nil {
		return fmt.Errorf("read observation requires fresh license initialization")
	}
	_, exists, err := keyvalue.Get(stateKey)
	if err != nil {
		return fmt.Errorf("read observation license precheck: %w", err)
	}
	if exists {
		return fmt.Errorf("read observation requires an absent initial license state")
	}
	if err := ctx.Err(); err != nil {
		return err
	}

	s, err := db.NewReadObservationReadSession(ctx)
	if err != nil {
		return fmt.Errorf("read observation license session: %w", err)
	}
	statuses := make([]Status, 0, 2)
	queryErr := ctx.Err()
	if queryErr == nil {
		queryErr = s.Cols("id", "instance_id").Limit(2).Find(&statuses)
	}
	closeErr := s.Close()
	if err := errors.Join(queryErr, closeErr, ctx.Err()); err != nil {
		return fmt.Errorf("read observation license status read: %w", err)
	}
	if len(statuses) != 1 || statuses[0].ID <= 0 || strings.TrimSpace(statuses[0].InstanceID) == "" {
		return fmt.Errorf("read observation requires one existing license instance")
	}

	_, exists, err = keyvalue.Get(stateKey)
	if err != nil {
		return fmt.Errorf("read observation license freshness: %w", err)
	}
	if exists {
		return fmt.Errorf("read observation license state changed during initialization")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	// degradeToFree owns stateMu and keeps the existing FREE entitlement semantics.
	degradeToFree("No license key configured.")
	raw, exists, err := keyvalue.Get(stateKey)
	if err != nil {
		return fmt.Errorf("read observation FREE readback: %w", err)
	}
	st, valid := raw.(state)
	if !exists || !valid || st.Licensed || st.Features == nil || len(st.Features) != 0 ||
		st.MaxUsers != 0 || !st.ExpiresAt.IsZero() || !st.LastCheckFailed {
		return fmt.Errorf("read observation FREE state was not confirmed")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	instanceID = statuses[0].InstanceID
	return nil
}
