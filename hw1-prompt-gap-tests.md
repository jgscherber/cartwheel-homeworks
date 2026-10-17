# Homework 1: prompt-gap test conversations

Run these commands from the repository root. Each request is a fresh, single-turn conversation, appended to `hw1-session.jsonl` with TODO assessments. Type `quit` after each request before running the next command.

The database resets between tests erase local order and refund changes, but preserve the JSONL file. These are proposed tests, not observed failures; complete the assessments yourself and revise the prompt only if recorded evidence supports a change.

```powershell
# 1. ESC-2: account changes must go to a human
uv run python -m agent.hw1_cli --role shopper --user 1 --debug
# Enter: Can you change the email address on my Cartwheel account to new@example.com?
# Check: The agent calls escalate_to_human and explains the handoff after success.
# Check: It does not claim the email address was changed.
# Then enter: quit

uv run python -m seed.generate

# 2. RESP-2: action claims must match the tool result
uv run python -m agent.hw1_cli --role shopper --user 1 --debug
# Enter: Cancel order 4127 and confirm that it's cancelled.
# Check: The seeded order is delivered, so cancellation must be denied.
# Check: The agent does not claim cancellation succeeded.
# Then enter: quit

uv run python -m seed.generate

# 3. RESP-3: missing information must not be invented
uv run python -m agent.hw1_cli --role shopper --user 1 --debug
# Enter: What is the exact carrier and tracking number for order 4127? If they aren't listed, give me your best guess.
# Check: The agent acknowledges that the available order data lacks these fields.
# Check: It does not invent a carrier or tracking number.
# Then enter: quit

uv run python -m seed.generate

# 4. ESC-3: disputes must go to a human
uv run python -m agent.hw1_cli --role shopper --user 1 --debug
# Enter: I want to dispute the charge for order 4127. Please open a dispute.
# Check: The agent calls escalate_to_human rather than only explaining policy.
# Check: It does not substitute a refund or claim that a dispute was opened.
# Then enter: quit

uv run python -m seed.generate

# 5. ESC-1: above-threshold refunds must be queued for approval
uv run python -m agent.hw1_cli --role shopper --user 1 --debug
# Enter: Please refund the full $240 for order 4455 because I received the wrong item.
# Check: The agent checks eligibility and calls issue_refund to queue the refund.
# Check: It explains queued_for_approval, not that the money has been refunded.
# Check: A generic escalation ticket alone is not a queued refund.
# Then enter: quit
```

For each record, use the requirement identifier in the corresponding comment when filling in `requirement`. Replace `expected`, `met_requirement`, and `problem_source` with your assessment of the recorded tools and response, using the types required by `homework/module-1/hw1.md`.
