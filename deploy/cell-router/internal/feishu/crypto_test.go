package feishu

import "testing"

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
