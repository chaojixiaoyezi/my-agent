package feishu

import (
	"cellrouter/internal/directory"
	"encoding/json"
	"net/http"
	"testing"
	"time"
)

func TestOwnerOfCardAndMessageRouting(t *testing.T) {
	cases := []struct {
		name      string
		card      bool
		eventBody map[string]any
		wantKind  string
		wantID    string
		wantTen   string
	}{
		{
			name: "p2p_message_event",
			card: false,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_1"},
				"event": map[string]any{
					"message": map[string]any{"chat_type": "p2p", "chat_id": "oc_msg_p2p"},
					"sender": map[string]any{
						"sender_id": map[string]any{"open_id": "ou_msg_1"},
					},
				},
			},
			wantKind: "user",
			wantID:   "ou_msg_1",
			wantTen:  "t_1",
		},
		{
			name: "group_message_event",
			card: false,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_1"},
				"event": map[string]any{
					"message": map[string]any{"chat_type": "group", "chat_id": "oc_msg_grp"},
					"sender": map[string]any{
						"sender_id": map[string]any{"open_id": "ou_msg_2"},
					},
				},
			},
			wantKind: "group",
			wantID:   "oc_msg_grp",
			wantTen:  "t_1",
		},
		{
			name: "p2p_card_callback_prefers_operator_open_id_over_open_chat_id",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_2"},
				"event": map[string]any{
					"chat_type": "p2p",
					"operator":  map[string]any{"open_id": "ou_card_p2p"},
					"context":   map[string]any{"open_chat_id": "oc_p2p_chat", "open_message_id": "om_1"},
					"action":    map[string]any{"value": map[string]any{"k": "v"}},
				},
			},
			wantKind: "user",
			wantID:   "ou_card_p2p",
			wantTen:  "t_2",
		},
		{
			name: "private_card_callback_in_context_uses_operator_id",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_2"},
				"event": map[string]any{
					"operator": map[string]any{
						"operator_id": map[string]any{"open_id": "ou_priv_op"},
					},
					"context": map[string]any{
						"chat_type":    " Private ",
						"open_chat_id": "oc_priv_chat",
					},
				},
			},
			wantKind: "user",
			wantID:   "ou_priv_op",
			wantTen:  "t_2",
		},
		{
			name: "private_password_card_pwd_set_routes_to_user_with_open_chat_id",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_pwd"},
				"event": map[string]any{
					"operator": map[string]any{"open_id": "ou_pwd_user"},
					"context":  map[string]any{"open_chat_id": "oc_dm_bind", "open_message_id": "om_pwd_1"},
					"action": map[string]any{
						"value": map[string]any{
							"session_lock_action": "pwd_set",
							"user_id":             "ou_pwd_user",
							"reference":           "ref_set_1",
						},
					},
				},
			},
			wantKind: "user",
			wantID:   "ou_pwd_user",
			wantTen:  "t_pwd",
		},
		{
			name: "private_password_card_pwd_unlock_routes_to_user",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_pwd"},
				"event": map[string]any{
					"operator": map[string]any{"open_id": "ou_pwd_user"},
					"context":  map[string]any{"open_chat_id": "oc_dm_bind"},
					"action": map[string]any{
						"value": map[string]any{
							"session_lock_action": "pwd_unlock",
							"user_id":             "ou_pwd_user",
							"reference":           "ref_unlock_1",
						},
					},
				},
			},
			wantKind: "user",
			wantID:   "ou_pwd_user",
			wantTen:  "t_pwd",
		},
		{
			name: "private_password_card_pwd_change_routes_to_user",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_pwd"},
				"event": map[string]any{
					"operator": map[string]any{"open_id": "ou_pwd_user"},
					"context":  map[string]any{"open_chat_id": "oc_dm_bind"},
					"action": map[string]any{
						"value": map[string]any{
							"session_lock_action": "pwd_change",
							"user_id":             "ou_pwd_user",
							"reference":           "ref_change_1",
						},
					},
				},
			},
			wantKind: "user",
			wantID:   "ou_pwd_user",
			wantTen:  "t_pwd",
		},
		{
			name: "private_password_card_pwd_cancel_routes_to_user",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_pwd"},
				"event": map[string]any{
					"operator": map[string]any{"open_id": "ou_pwd_user"},
					"context":  map[string]any{"open_chat_id": "oc_dm_bind"},
					"action": map[string]any{
						"value": map[string]any{
							"session_lock_action": "pwd_cancel",
							"user_id":             "ou_pwd_user",
							"reference":           "ref_cancel_1",
						},
					},
				},
			},
			wantKind: "user",
			wantID:   "ou_pwd_user",
			wantTen:  "t_pwd",
		},
		{
			name: "password_card_mismatched_user_id_does_not_take_personal_route",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_pwd"},
				"event": map[string]any{
					"operator": map[string]any{"open_id": "ou_other"},
					"context":  map[string]any{"open_chat_id": "oc_some_chat"},
					"action": map[string]any{
						"value": map[string]any{
							"session_lock_action": "pwd_unlock",
							"user_id":             "ou_pwd_user",
							"reference":           "ref_unlock_1",
						},
					},
				},
			},
			wantKind: "group",
			wantID:   "oc_some_chat",
			wantTen:  "t_pwd",
		},
		{
			name: "password_card_missing_reference_does_not_take_personal_route",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_pwd"},
				"event": map[string]any{
					"operator": map[string]any{"open_id": "ou_pwd_user"},
					"context":  map[string]any{"open_chat_id": "oc_some_chat"},
					"action": map[string]any{
						"value": map[string]any{
							"session_lock_action": "pwd_unlock",
							"user_id":             "ou_pwd_user",
							"reference":           "",
						},
					},
				},
			},
			wantKind: "group",
			wantID:   "oc_some_chat",
			wantTen:  "t_pwd",
		},
		{
			name: "non_password_group_card_without_chat_type_routes_to_group",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_3"},
				"event": map[string]any{
					"operator": map[string]any{"open_id": "ou_group_actor"},
					"context":  map[string]any{"open_chat_id": "oc_grp_no_type"},
					"action":   map[string]any{"value": map[string]any{"kind": "approval"}},
				},
			},
			wantKind: "group",
			wantID:   "oc_grp_no_type",
			wantTen:  "t_3",
		},
		{
			name: "explicit_group_card_callback_via_chat_type",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_4"},
				"event": map[string]any{
					"operator": map[string]any{"open_id": "ou_grp_actor"},
					"context": map[string]any{
						"chat_type":    "group",
						"open_chat_id": "oc_grp_card",
					},
				},
			},
			wantKind: "group",
			wantID:   "oc_grp_card",
			wantTen:  "t_4",
		},
		{
			name: "explicit_group_card_callback_via_chat_mode",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_4"},
				"event": map[string]any{
					"operator": map[string]any{"open_id": "ou_grp_actor2"},
					"context": map[string]any{
						"chat_mode":    "GROUP",
						"open_chat_id": "oc_mode_grp",
					},
				},
			},
			wantKind: "group",
			wantID:   "oc_mode_grp",
			wantTen:  "t_4",
		},
		{
			name: "card_callback_fallback_when_open_id_empty",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_5"},
				"event": map[string]any{
					"context": map[string]any{"open_chat_id": "oc_legacy_grp"},
				},
			},
			wantKind: "group",
			wantID:   "oc_legacy_grp",
			wantTen:  "t_5",
		},
		{
			name: "card_callback_without_open_chat_id_routes_to_operator",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_5b"},
				"event": map[string]any{
					"operator": map[string]any{"open_id": "ou_only_op"},
				},
			},
			wantKind: "user",
			wantID:   "ou_only_op",
			wantTen:  "t_5b",
		},
		{
			name: "p2p_card_callback_without_open_id_never_misroutes_to_group",
			card: true,
			eventBody: map[string]any{
				"header": map[string]any{"tenant_key": "t_6"},
				"event": map[string]any{
					"chat_type": "p2p",
					"context":   map[string]any{"open_chat_id": "oc_p2p_only"},
				},
			},
			wantKind: "unknown",
			wantID:   "",
			wantTen:  "t_6",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := ownerOf(tc.eventBody, tc.card)
			if got.kind != tc.wantKind || got.id != tc.wantID || got.tenant != tc.wantTen {
				t.Fatalf("ownerOf(..., card=%v) = %+v, want {tenant:%q kind:%q id:%q}", tc.card, got, tc.wantTen, tc.wantKind, tc.wantID)
			}
		})
	}
}

func TestPasswordCardActionStringAndMapParityThroughDecode(t *testing.T) {
	cred := directory.Credentials{VerificationToken: "vt_test_token"}
	now := time.Unix(1700000000, 0)
	actions := []string{"pwd_set", "pwd_unlock", "pwd_change", "pwd_cancel"}

	for _, act := range actions {
		valObj := map[string]any{
			"session_lock_action": act,
			"user_id":             "ou_pwd_user",
			"reference":           "ref_" + act,
		}
		valBytes, err := json.Marshal(valObj)
		if err != nil {
			t.Fatalf("marshal value: %v", err)
		}

		for _, mode := range []string{"map", "json_string", "padded_json_string"} {
			t.Run(act+"_"+mode, func(t *testing.T) {
				var rawValue any = valObj
				if mode == "json_string" {
					rawValue = string(valBytes)
				} else if mode == "padded_json_string" {
					rawValue = "  " + string(valBytes) + "\n"
				}
				wireMap := map[string]any{
					"token":  cred.VerificationToken,
					"header": map[string]any{"tenant_key": "t_pwd"},
					"event": map[string]any{
						"operator": map[string]any{"open_id": "ou_pwd_user"},
						"context":  map[string]any{"open_chat_id": "oc_dm_bind", "open_message_id": "om_pwd_1"},
						"action":   map[string]any{"value": rawValue},
					},
				}
				rawBytes, err := json.Marshal(wireMap)
				if err != nil {
					t.Fatalf("marshal wire event: %v", err)
				}
				decoded, err := Decode(rawBytes, http.Header{}, cred, now, 5*time.Minute)
				if err != nil {
					t.Fatalf("Decode failed: %v", err)
				}
				if !isCard(decoded) {
					t.Fatalf("isCard(decoded) = false, want true")
				}
				got := ownerOf(decoded, true)
				if got.kind != "user" || got.id != "ou_pwd_user" || got.tenant != "t_pwd" {
					t.Fatalf("ownerOf(decoded, true) = %+v, want {tenant:\"t_pwd\" kind:\"user\" id:\"ou_pwd_user\"}", got)
				}
			})
		}
	}

	negativeCases := []struct {
		name       string
		chatType   string
		chatMode   string
		openID     string
		rawValue   any
		wantIsCard bool
		wantKind   string
		wantID     string
	}{
		{
			name:       "malformed_json_string",
			openID:     "ou_pwd_user",
			rawValue:   "{not-json",
			wantIsCard: false,
			wantKind:   "group",
			wantID:     "oc_some_chat",
		},
		{
			name:       "json_null_string",
			openID:     "ou_pwd_user",
			rawValue:   "null",
			wantIsCard: false,
			wantKind:   "group",
			wantID:     "oc_some_chat",
		},
		{
			name:       "json_array_string",
			openID:     "ou_pwd_user",
			rawValue:   "[]",
			wantIsCard: false,
			wantKind:   "group",
			wantID:     "oc_some_chat",
		},
		{
			name:       "json_number_string",
			openID:     "ou_pwd_user",
			rawValue:   "123",
			wantIsCard: false,
			wantKind:   "group",
			wantID:     "oc_some_chat",
		},
		{
			name:       "non_password_action_string",
			openID:     "ou_pwd_user",
			rawValue:   `{"kind":"approval"}`,
			wantIsCard: true,
			wantKind:   "group",
			wantID:     "oc_some_chat",
		},
		{
			name:       "unknown_session_lock_action_string",
			openID:     "ou_pwd_user",
			rawValue:   `{"session_lock_action":"pwd_other","user_id":"ou_pwd_user","reference":"ref_1"}`,
			wantIsCard: true,
			wantKind:   "group",
			wantID:     "oc_some_chat",
		},
		{
			name:       "missing_reference_in_string",
			openID:     "ou_pwd_user",
			rawValue:   `{"session_lock_action":"pwd_unlock","user_id":"ou_pwd_user"}`,
			wantIsCard: true,
			wantKind:   "group",
			wantID:     "oc_some_chat",
		},
		{
			name:       "mismatched_open_id_in_string",
			openID:     "ou_other",
			rawValue:   `{"session_lock_action":"pwd_unlock","user_id":"ou_pwd_user","reference":"ref_1"}`,
			wantIsCard: true,
			wantKind:   "group",
			wantID:     "oc_some_chat",
		},
		{
			name:       "explicit_group_chat_type_overrides_password_card_string",
			chatType:   "group",
			openID:     "ou_pwd_user",
			rawValue:   `{"session_lock_action":"pwd_unlock","user_id":"ou_pwd_user","reference":"ref_1"}`,
			wantIsCard: true,
			wantKind:   "group",
			wantID:     "oc_some_chat",
		},
		{
			name:       "explicit_group_chat_mode_overrides_password_card_string",
			chatMode:   "group",
			openID:     "ou_pwd_user",
			rawValue:   `{"session_lock_action":"pwd_set","user_id":"ou_pwd_user","reference":"ref_1"}`,
			wantIsCard: true,
			wantKind:   "group",
			wantID:     "oc_some_chat",
		},
	}

	for _, tc := range negativeCases {
		t.Run(tc.name, func(t *testing.T) {
			ctx := map[string]any{"open_chat_id": "oc_some_chat"}
			if tc.chatType != "" {
				ctx["chat_type"] = tc.chatType
			}
			if tc.chatMode != "" {
				ctx["chat_mode"] = tc.chatMode
			}
			wireMap := map[string]any{
				"token":  cred.VerificationToken,
				"header": map[string]any{"tenant_key": "t_pwd"},
				"event": map[string]any{
					"operator": map[string]any{"open_id": tc.openID},
					"context":  ctx,
					"action":   map[string]any{"value": tc.rawValue},
				},
			}
			rawBytes, err := json.Marshal(wireMap)
			if err != nil {
				t.Fatalf("marshal negative wire event: %v", err)
			}
			decoded, err := Decode(rawBytes, http.Header{}, cred, now, 5*time.Minute)
			if err != nil {
				t.Fatalf("Decode failed: %v", err)
			}
			if gotCard := isCard(decoded); gotCard != tc.wantIsCard {
				t.Fatalf("isCard(decoded) = %v, want %v", gotCard, tc.wantIsCard)
			}
			ev := object(decoded, "event")
			if isPrivatePasswordCardAction(ev, tc.openID) {
				if tc.chatType == "" && tc.chatMode == "" {
					t.Fatalf("isPrivatePasswordCardAction(ev, %q) = true, want false", tc.openID)
				}
			}
			got := ownerOf(decoded, true)
			if got.kind != tc.wantKind || got.id != tc.wantID || got.tenant != "t_pwd" {
				t.Fatalf("ownerOf(decoded, true) = %+v, want {tenant:\"t_pwd\" kind:%q id:%q}", got, tc.wantKind, tc.wantID)
			}
		})
	}
	if isPrivatePasswordCardAction(map[string]any{"action": map[string]any{"value": `{"session_lock_action":"pwd_unlock","user_id":"","reference":"ref_1"}`}}, "") {
		t.Fatalf("isPrivatePasswordCardAction with empty openID = true, want false")
	}
}
