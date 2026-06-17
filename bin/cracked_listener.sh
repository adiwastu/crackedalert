#!/usr/bin/env bash

# ==========================================
# CRACKED ALERT SYSTEM - LISTENER DAEMON
# ==========================================

# --- CONFIG & PATHS ---
WORK_DIR="/etc/cracked_alert"
ENV_FILE="${WORK_DIR}/.env_cracked"
DB_FILE="${WORK_DIR}/cracked_alerts.tsv"
OFFSET_FILE="${WORK_DIR}/.tg_offset"

mkdir -p "$WORK_DIR"
touch "$DB_FILE"
touch "$OFFSET_FILE"

# --- LOAD SECRETS ---
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
else
    echo "ERROR: .env_cracked not found at $ENV_FILE"
    exit 1
fi

# --- MT5 ACCOUNT API ROUTING ---
# Associative array: maps account shortcodes to their base API URLs.
# Must be declared here in global scope so it's available throughout the script.
declare -A API_URLS
API_URLS["5k"]="https://api.hotland3x3.my.id"
API_URLS["10k"]="https://api-5ers.hotland3x3.my.id"
API_URLS["raven"]="https://api-raven.hotland3x3.my.id"
API_URLS["demo"]="https://api-demo.hotland3x3.my.id"

# --- HELPER: SEND TELEGRAM MESSAGE ---
send_msg() {
    local CHAT_ID="$1"
    local MSG="$2"
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${CHAT_ID}" \
        -d text="${MSG}" \
        -d parse_mode="HTML" > /dev/null
}

echo "[Cracked Listener] Started. Polling Telegram..."

# --- MAIN LONG-POLLING LOOP ---
while true; do
    # 1. ALWAYS force read the latest offset from disk before calling Telegram
    OFFSET=$(cat "$OFFSET_FILE" 2>/dev/null)
    [ -z "$OFFSET" ] && OFFSET=0

    # Fetch updates with a 100-second timeout to hold the connection open
    UPDATES=$(curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" \
        -d "offset=${OFFSET}" \
        -d "timeout=100")

    # Check if we got a valid JSON response with updates
    HAS_UPDATES=$(echo "$UPDATES" | jq -r '.ok')
    
    if [ "$HAS_UPDATES" == "true" ]; then
        # Parse updates using jq and iterate through them
        echo "$UPDATES" | jq -c '.result[] | select(.message.text != null) | {update_id: .update_id, chat_id: .message.chat.id, text: .message.text}' | while read -r ROW; do
            
            UPDATE_ID=$(echo "$ROW" | jq -r '.update_id')
            CHAT_ID=$(echo "$ROW" | jq -r '.chat_id')
            RAW_TEXT=$(echo "$ROW" | jq -r '.text')

            # Update offset to acknowledge message
            OFFSET=$((UPDATE_ID + 1))
            echo "$OFFSET" > "$OFFSET_FILE"

            # ==========================================
            # COMMAND ROUTER
            # Uses if/elif chain to dispatch each command.
            # Order matters: /m and /p checked before more
            # specific patterns to avoid false matches.
            # ==========================================

            # ==========================================
            # MARKET & PENDING ORDER EXECUTION (XAUUSD ONLY)
            # ==========================================
            if [[ "$RAW_TEXT" == /m* ]] || [[ "$RAW_TEXT" == /p* ]]; then
                
                # Hardcode Symbol
                SYMBOL="XAUUSD"

                # Parse depending on /m or /p
                # /m does not take an entry price (uses live market price)
                # /p takes an explicit pending entry price as the first arg
                if [[ "$RAW_TEXT" == /m* ]]; then
                    read -r CMD TARGET_SL WIDEN RR RISK ACCT <<< "$RAW_TEXT"
                    TARGET_ENTRY="MARKET"
                else
                    read -r CMD TARGET_ENTRY TARGET_SL WIDEN RR RISK ACCT <<< "$RAW_TEXT"
                fi

                # Validate Account string against the API_URLS associative array
                BASE_URL="${API_URLS[$ACCT]}"
                if [ -z "$BASE_URL" ]; then
                    send_msg "$CHAT_ID" "error: account '${ACCT}' not found."
                    continue
                fi

                # Fetch Account Balance
                BALANCE=$(curl -s "$BASE_URL/account" | jq -r '.data.balance')
                if [ -z "$BALANCE" ] || [ "$BALANCE" == "null" ]; then
                    send_msg "$CHAT_ID" "error: could not fetch balance for ${ACCT}."
                    continue
                fi

                # Fetch Live Price
                LIVE_PRICE=$(curl -s "$BASE_URL/fetch_data_pos?symbol=${SYMBOL}&timeframe=M1&num_bars=1" | jq -r '.[-1].close')

                # Determine baseline entry for math
                # If MARKET order, use live price as the math entry reference
                MATH_ENTRY=$TARGET_ENTRY
                [ "$TARGET_ENTRY" == "MARKET" ] && MATH_ENTRY=$LIVE_PRICE

                # AWK Math Engine (Pure XAUUSD Logic)
                # AWK is used here as an embedded arithmetic co-processor because
                # Bash itself cannot do floating-point math natively.
                # The -v flags pass shell variables into AWK's scope.
                CALCS=$(awk -v entry="$MATH_ENTRY" -v sl="$TARGET_SL" -v rr="$RR" -v risk_pct="$RISK" -v widen="$WIDEN" -v bal="$BALANCE" '
                    BEGIN {
                        # 1. Infer Direction
                        if (sl < entry) dir = "BUY"; else dir = "SELL";
                        
                        # 2. XAUUSD SL Widening (3.00 absolute = 30 pips)
                        widen_text = "";
                        if (widen == "y" || widen == "Y") {
                            if (dir == "BUY") sl = sl - 3.00;
                            else sl = sl + 3.00;
                            widen_text = " (tambah 30 pips)";
                        }

                        # 3. Distance & TP Calc
                        dist = entry - sl; 
                        if (dist < 0) dist = -dist;
                        
                        if (dir == "BUY") tp = entry + (dist * rr);
                        else tp = entry - (dist * rr);

                        # 4. Risk & Lot Calc (XAUUSD Contract = 100 oz)
                        risk_usd = bal * (risk_pct / 100);
                        if (dist > 0) {
                            lots = risk_usd / (dist * 100);
                            if (lots < 0.01) lots = 0.01;
                        } else {
                            lots = 0;
                        }
                        
                        printf "%s|%.2f|%.2f|%.2f|%.2f|%s", dir, sl, tp, lots, risk_usd, widen_text
                    }
                ')

                # IFS splitting: temporarily change the Internal Field Separator
                # to '|' so read can unpack the pipe-delimited AWK output
                # into individual named variables in one shot.
                IFS='|' read -r EXEC_DIR EXEC_SL EXEC_TP EXEC_LOTS RISK_USD WIDEN_LBL <<< "$CALCS"

                if (( $(echo "$EXEC_LOTS <= 0" | bc -l) )); then
                    send_msg "$CHAT_ID" "error: lot size calculated to 0. check parameters."
                    continue
                fi

                # Build JSON Payload
                # jq -n (null input) is used to safely construct JSON from shell variables,
                # avoiding injection issues that would come from string interpolation.
                if [ "$TARGET_ENTRY" == "MARKET" ]; then
                    JSON_PAYLOAD=$(jq -n \
                        --arg sym "$SYMBOL" \
                        --arg type "$EXEC_DIR" \
                        --argjson vol "$EXEC_LOTS" \
                        --argjson sl "$EXEC_SL" \
                        --argjson tp "$EXEC_TP" \
                        '{symbol: $sym, type: $type, volume: $vol, sl: $sl, tp: $tp, magic: 777}')
                    ORDER_TYPE_LBL="MARKET"
                else
                    JSON_PAYLOAD=$(jq -n \
                        --arg sym "$SYMBOL" \
                        --arg type "$EXEC_DIR" \
                        --argjson vol "$EXEC_LOTS" \
                        --argjson price "$TARGET_ENTRY" \
                        --argjson sl "$EXEC_SL" \
                        --argjson tp "$EXEC_TP" \
                        '{symbol: $sym, type: $type, volume: $vol, price: $price, sl: $sl, tp: $tp, magic: 777}')
                    ORDER_TYPE_LBL="PENDING"
                fi

                # Execute Order via POST
                API_RESP=$(curl -s -X POST "$BASE_URL/order" -H "Content-Type: application/json" -d "$JSON_PAYLOAD")
                
                ERR_MSG=$(echo "$API_RESP" | jq -r '.message // empty')
                TICKET=$(echo "$API_RESP" | jq -r '.result.order // empty')

                if [ -n "$TICKET" ] && [ "$TICKET" != "null" ]; then
                    SUCCESS_MSG="order placed (ticket: #${TICKET})
${SYMBOL} - ${EXEC_DIR} ${ORDER_TYPE_LBL} (${ACCT})
lots: ${EXEC_LOTS} (${RISK}% risk = \$${RISK_USD})

entry: ${MATH_ENTRY}
sl: ${EXEC_SL}${WIDEN_LBL}
tp: ${EXEC_TP} (1:${RR} RR)"
                    send_msg "$CHAT_ID" "$SUCCESS_MSG"
                else
                    RETCODE=$(echo "$API_RESP" | jq -r '.result.retcode // "unknown"')
                    FAIL_MSG="order failed (${ACCT})
${SYMBOL} - ${EXEC_DIR} ${ORDER_TYPE_LBL}
reason: ${ERR_MSG} (retcode: ${RETCODE})"
                    send_msg "$CHAT_ID" "$FAIL_MSG"
                fi

            # ==========================================
            # Command: /alert
            # ==========================================
            elif [[ "$RAW_TEXT" == /alert* ]]; then
                # Parse arguments: /alert <PRICE> [SYMBOL] [MESSAGE]
                read -r CMD TARGET_PRICE SYMBOL MSG <<< "$RAW_TEXT"
                
                # Validation
                if [ -z "$TARGET_PRICE" ]; then
                    send_msg "$CHAT_ID" "⚠️ <b>Usage:</b> /alert 2450.00 XAUUSD Approaching support"
                    continue
                fi

                # Defaults
                [ -z "$SYMBOL" ] && SYMBOL="XAUUSD"
                [ -z "$MSG" ] && MSG="Price target reached."
                
                # Fetch live price to lock in directional logic
                API_URL="https://api.hotland3x3.my.id/fetch_data_pos?symbol=${SYMBOL}&timeframe=M1&num_bars=1"
                LIVE_PRICE=$(curl -s "$API_URL" | jq -r '.[-1].close')

                if [ -z "$LIVE_PRICE" ] || [ "$LIVE_PRICE" == "null" ]; then
                    send_msg "$CHAT_ID" "❌ API Error: Could not fetch live price for $SYMBOL."
                    continue
                fi

                # Determine direction using awk for floating point math
                DIRECTION=$(awk -v live="$LIVE_PRICE" -v target="$TARGET_PRICE" 'BEGIN { if (live < target) print "CROSSING_UP"; else print "CROSSING_DOWN" }')
                
                # Generate a short 4-character alphanumeric ID safely (CPU leak patched)
                ALERT_ID=$(head -c 100 /dev/urandom | tr -dc 'A-Z0-9' | head -c 4)

                # Write to flat database (TSV format)
                echo -e "${ALERT_ID}\t${CHAT_ID}\t${SYMBOL}\t${TARGET_PRICE}\t${DIRECTION}\t${MSG}" >> "$DB_FILE"

                # Determine higher/lower relation for the confirm message
                if [ "$DIRECTION" == "CROSSING_UP" ]; then
                    RELATION="lower"
                else
                    RELATION="higher"
                fi

                CONFIRM_MSG="cracked alert set (id: ${ALERT_ID}).
${SYMBOL}
${TARGET_PRICE}

Notes: ${MSG}
current price (${LIVE_PRICE}) is now ${RELATION} than target."

                send_msg "$CHAT_ID" "$CONFIRM_MSG"
                echo "Logged Alert: $ALERT_ID | $SYMBOL | $TARGET_PRICE | $DIRECTION"

            # ==========================================
            # Command: /list
            # ==========================================
            elif [[ "$RAW_TEXT" == /list* ]]; then
                ACTIVE_COUNT=$(grep -c "^[A-Z0-9].*${CHAT_ID}" "$DB_FILE" 2>/dev/null || echo "0")
                if [ "$ACTIVE_COUNT" -eq 0 ]; then
                    send_msg "$CHAT_ID" "no active alerts."
                else
                    LIST_MSG="active alerts:"
                    
                    while IFS=$'\t' read -r ID C_ID SYM TGT DIR C_MSG; do
                        if [ "$C_ID" == "$CHAT_ID" ]; then
                            LIST_MSG="${LIST_MSG}
(${ID})  ${SYM} @ ${TGT} - ${C_MSG}"
                        fi
                    done < "$DB_FILE"
                    
                    send_msg "$CHAT_ID" "$LIST_MSG"
                fi

            # ==========================================
            # Command: /cancel
            # ==========================================
            elif [[ "$RAW_TEXT" == /cancel* ]]; then
                DEL_ID=$(echo "$RAW_TEXT" | awk '{print $2}')
                if [ -z "$DEL_ID" ]; then
                    send_msg "$CHAT_ID" "⚠️ Usage: /cancel <ID>"
                    continue
                fi

                # ANSI-C quoting ($'...') is used to produce a literal tab character
                # that grep can use as a field delimiter without ambiguity
                TAB=$'\t'
                
                if grep -q "^${DEL_ID}${TAB}${CHAT_ID}" "$DB_FILE"; then
                    sed -i "/^${DEL_ID}${TAB}${CHAT_ID}/d" "$DB_FILE"
                    send_msg "$CHAT_ID" "alert ${DEL_ID} cancelled."
                else
                    send_msg "$CHAT_ID" "id ${DEL_ID} not found or doesn't belong to you."
                fi

            # ==========================================
            # Command: /help
            # ==========================================
            elif [[ "$RAW_TEXT" == /help* ]]; then
                HELP_MSG="cracked alert commands:

market execution:
/m [sl] [widen:y/n] [rr] [risk%] [account]
example: /m 2440.00 y 2 0.5 10k

pending execution:
/p [entry] [sl] [widen:y/n] [rr] [risk%] [account]
example: /p 2450.00 2455.00 n 3 1 5k

set alert:
/alert [target] [notes]
example: /alert 2450.00 approaching demand

utilities:
/list — shows active alerts
/cancel [id] — deletes alert
/help — shows this message

accounts: 5k | 10k | raven | demo"
                send_msg "$CHAT_ID" "$HELP_MSG"
            fi

        done
    fi

    # Small sleep to prevent aggressive spinning if curl fails instantly
    sleep 1
done