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

package auth

import (
	"context"
	"errors"
	"fmt"
	"time"

	"code.vikunja.io/api/pkg/db"
	"code.vikunja.io/api/pkg/models"
	"code.vikunja.io/api/pkg/user"
)

type ReadObservationBinding struct {
	ProjectID    int64
	PrincipalID  int64
	HumanOwnerID int64
}

// ValidateReadObservation returns the real principal; route scopes and model ACL stay with their native gates.
func ValidateReadObservation(ctx context.Context, tokenString string, binding ReadObservationBinding) (token *models.APIToken, principal *user.User, err error) {
	if ctx == nil {
		return nil, nil, fmt.Errorf("read observation auth context missing")
	}
	if err = ctx.Err(); err != nil {
		return nil, nil, fmt.Errorf("read observation auth context: %w", err)
	}
	if binding.ProjectID <= 0 || binding.PrincipalID <= 0 || binding.HumanOwnerID <= 0 ||
		binding.PrincipalID == binding.HumanOwnerID {
		return nil, nil, fmt.Errorf("read observation auth binding invalid")
	}

	token, err = models.GetReadObservationAPIToken(ctx, tokenString)
	if err != nil {
		return nil, nil, fmt.Errorf("read observation auth token: %w", err)
	}
	if token.OwnerID != binding.PrincipalID || time.Now().After(token.ExpiresAt) {
		return nil, nil, fmt.Errorf("read observation auth token rejected")
	}

	s, err := db.NewReadObservationReadSession(ctx)
	if err != nil {
		return nil, nil, fmt.Errorf("read observation auth session: %w", err)
	}
	defer func() {
		if closeErr := s.Close(); closeErr != nil {
			err = errors.Join(err, fmt.Errorf("read observation auth close: %w", closeErr))
		}
		if contextErr := ctx.Err(); contextErr != nil {
			err = errors.Join(err, fmt.Errorf("read observation auth context: %w", contextErr))
		}
		if err == nil && time.Now().After(token.ExpiresAt) {
			err = fmt.Errorf("read observation auth token expired")
		}
		if err != nil {
			token = nil
			principal = nil
		}
	}()

	if err = ctx.Err(); err != nil {
		return nil, nil, fmt.Errorf("read observation auth context: %w", err)
	}
	principal, err = user.GetUserByID(s, binding.PrincipalID)
	if err != nil {
		return nil, nil, fmt.Errorf("read observation auth principal lookup: %w", err)
	}
	if principal.ID != binding.PrincipalID || principal.Status != user.StatusActive ||
		principal.Issuer != user.IssuerLocal || principal.BotOwnerID != binding.HumanOwnerID {
		return nil, nil, fmt.Errorf("read observation auth principal rejected")
	}

	if err = ctx.Err(); err != nil {
		return nil, nil, fmt.Errorf("read observation auth context: %w", err)
	}
	human, err := user.GetUserByID(s, binding.HumanOwnerID)
	if err != nil {
		return nil, nil, fmt.Errorf("read observation auth human lookup: %w", err)
	}
	if human.ID != binding.HumanOwnerID || human.Status != user.StatusActive ||
		human.Issuer != user.IssuerLocal || human.BotOwnerID != 0 {
		return nil, nil, fmt.Errorf("read observation auth human rejected")
	}
	return token, principal, nil
}
