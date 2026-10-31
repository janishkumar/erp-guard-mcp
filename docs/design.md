# po-guard-mcp: design

Author: Janish Kumar
Status: agreed 2026-10-29, build started

## Problem

Leakage in procure-to-pay is cheaper to stop than to find. Duplicate supplier records, purchases split to stay under an approval limit, orders that blow a cost centre's budget, prices well above the last purchase, and requesters who approve their own orders all get caught (if at all) by after-the-fact audit, months later and at a few cents on the dollar recovered. Spend Leakage Finder is the detection side: it finds these patterns in 476K City of Chicago payments after the money has gone. po-guard-mcp is the prevention side.

AI assistants are starting to do procure-to-pay work directly in ERPs. An assistant with raw API access can be confused or prompt-injected (a supplier's notes field that says "approve this immediately") into skipping every control a human buyer would follow. Telling the model to be careful is not a control. po-guard-mcp is an MCP server whose write tools enforce the controls in code, so the same checks hold whatever the model was told.

## Goals

1. MCP tools for the core P2P writes: create a purchase order, onboard a supplier, submit for approval, approve or reject, cancel, amend. Plus read tools: supplier search, PO lookup, budget status, a read-only preview, and the audit log.
2. Every write follows preview → one-time confirmation code → write → audit record. The audit record lives inside the ERP.
3. Checks enforced in code on every write: duplicate supplier, split purchase, budget per cost centre, price vs last purchase, segregation of duties.
4. ERP-neutral guardrail core; each ERP plugs in through a connector.
5. A guardrail eval suite of attack scenarios with a measured count of unsafe writes that reached the ERP.

## Non-goals

- Never writes payments, bank-account details or deletions. There is no tool, connector method or HTTP path for them (see "Hard limits").
- No goods receipts (MIGO) or invoice posting (MIRO) in v1.
- Not a replacement for ERP-side authorisation. It adds controls in front of the API; ERP roles still apply.
- No real SAP tenant, no SAP CAL, nothing that touches a university SAP system.

## Architecture

```
MCP client (Claude)
   │  stdio
   ▼
po_guard.server        MCP tools, thin: parse args, call the service, format markdown
   │
po_guard.service       preview / commit orchestration, confirmation codes, audit
   │        │
   │   po_guard.checks  pure functions: (proposed action, ERP snapshot, policy) -> CheckResult[]
   │   po_guard.matching vendor-name normalisation + guarded fuzzy match (from Spend Leakage Finder)
   │   po_guard.policy   approval tiers, tolerances, windows (config/policy.toml)
   ▼
po_guard.connectors.base   ErpConnector protocol: reads + a small fixed set of writes
   ├── erpnext.py        ERPNext REST API (live, Docker on localhost)
   ├── sap.py            SAP S/4HANA Cloud OData (reads live from api.sap.com sandbox; writes fixture-tested)
   └── memory.py         in-memory ERP for unit tests and the fast eval run
```

The checks never call the ERP themselves. The service gathers what they need through the connector (supplier master, open POs for a supplier, cost-centre budget and commitments, last purchase price), then runs pure check functions. That keeps every check unit-testable and identical across ERPs.

## The write protocol

1. **Preview** (`preview` tool, read-only). Takes the write tool's name and the exact arguments it will be called with. The service canonicalises the arguments (sorted keys, normalised decimals), hashes them with the actor and action, runs every check, and returns: what would change, each check's result (pass / warn / block, with the evidence), and, only if nothing blocks, a one-time confirmation code.
2. **Write** (e.g. `create_purchase_order`, with `confirmation_code`). The service recomputes the hash from the arguments actually passed. The code must exist, be unused, unexpired (15 minutes), and bound to the same action, actor and hash. Any edit between preview and write invalidates it.
3. **Re-check.** All checks run again at write time against fresh ERP data, since another order may have used the budget in between. A code is evidence that a clean preview happened; it never overrides a block.
4. **Audit.** Every write-tool call is recorded in the ERP, including refused ones: action, actor, target, payload hash, check results, outcome (`written`, `blocked`, `refused`, `error`) and the ERP document it produced. In ERPNext this is a custom DocType, `PO Guard Audit`, which the MCP's users can create and read but not edit or delete.

Codes are random (from `secrets`), 8 characters from an unambiguous alphabet, held in server memory and burned on first use, success or not.

What the code does and doesn't prove: it proves the write matches a preview whose checks passed. It is not human approval. Human approval is the separate approve step, done by a different person under segregation of duties. MCP elicitation (the server asking the human directly) is a roadmap item.

## Identity

The acting user is fixed by the server's configuration (the ERP API key it runs with), never a tool argument. The connector confirms it with the ERP at start-up. A requester's session therefore can't approve "as" someone else, whatever the model is told.

## Checks

| Check | Runs on | Rule | Result |
|---|---|---|---|
| Duplicate supplier | onboard | Exact tax-ID match, or the name passes Spend Leakage Finder's guarded match (normalise, numbers must agree, token rules, ratio ≥ 85) against any existing supplier | block |
| | onboard | Looser name similarity (token-set ratio ≥ 90) without passing the guarded match | warn |
| Supplier usable | create, amend | Supplier exists, is enabled, not on hold | block |
| Self-dealing | create, approve | Requester (or approver) onboarded the supplier | block |
| Split purchase | create, submit, amend | This order is under an approval tier, but this order plus other live orders to the same supplier and cost centre in the last 14 days cross that tier | block |
| Budget | create, submit, amend, approve | Cost centre's committed spend this fiscal year (live orders) + this order > annual budget | block |
| Price vs last purchase | create, amend | Unit price above the item's last purchase price by > 10% | warn |
| | | by > 50% | block |
| Segregation of duties | approve, reject | Approver is not the requester | block |
| Approval authority | approve | Approver's limit ≥ order total | block |
| State | submit, approve, reject, cancel, amend | Order is in a state that allows the action | block |
| Instruction-like text | create, onboard, amend | Free-text fields contain instruction-like phrases ("ignore previous", "approve immediately", "system:") | warn |

The instruction-text warning is a convenience, not the defence. The defence is structural: free text is stored as data and never changes what a tool does, identity isn't an argument, and every write needs a clean preview.

Policy lives in `config/policy.toml`: approval tiers (manager to $50,000, director to $250,000, CFO above), the split window, price tolerances, the fiscal-year start. Users map to tiers there too.

## Hard limits

- The connector protocol has no methods for payments, bank details or deletion.
- The ERPNext HTTP client refuses DELETE, and refuses any doctype outside an allowlist (`Purchase Order`, `Supplier`, `Item`, `Cost Center`, `PO Guard Audit`, plus read-only lookups). Bank-related fields (`Bank Account`, IBAN, routing numbers) are stripped from supplier payloads and rejected if present.
- Tests assert all three, so a later change that adds such a path fails CI.

## ERP lifecycle mapping

| po-guard action | ERPNext | SAP S/4HANA (V4 Purchase Order API) | SAP GUI |
|---|---|---|---|
| onboard_supplier | Supplier (custom fields: tax ID, onboarded by) | Business Partner / supplier (A_Supplier) | BP / XK01 |
| create_purchase_order | Purchase Order, draft (docstatus 0), status Draft | POST PurchaseOrder with items (deep insert) | ME21N |
| submit_for_approval | custom status → Pending Approval | release strategy start | ME21N save → release |
| approve | submit document (docstatus 1) | release (ME29N) | ME29N |
| reject | custom status → Rejected (stays draft) | release refused | ME29N |
| cancel | cancel (docstatus 2) if approved; Withdrawn if draft | set deletion indicator on items | ME22N |
| amend | edit lines of an unapproved order, back to Draft | PATCH PurchaseOrderItem | ME22N |

Goods receipt (MIGO), invoice (MIRO) and payment run (F110) are out of scope; the last is out of scope permanently.

## Connectors

**ERPNext** (open source, GPL) runs locally in Docker from frappe_docker's single-file compose, image pinned to `v16.52.0`, bound to `127.0.0.1:8080`. `make up` starts it; `make seed` completes the setup wizard and loads the demo company, users and API keys (written to a gitignored `.env`); `make down` stops it. Live reads and writes through the REST API.

**SAP S/4HANA Cloud.** The free api.sap.com sandbox is read-only and needs an API key from a free SAP account. Reads (suppliers, purchase orders) run live when `SAP_API_KEY` is set. Writes are built against SAP's current V4 purchase-order API (`API_PURCHASEORDER_2`; the V2 `API_PURCHASEORDER_PROCESS_SRV` is deprecated): CSRF-token fetch, deep insert of header and items, ETag on update. They are tested against fixtures and labelled "not run against a live tenant". Until a key exists, read fixtures are built from SAP's published API schema, not recorded; they get replaced by recorded sandbox responses once a key is available. The SAP connector has no custom audit table to write to in the sandbox, so its audit records go to a local JSONL file, labelled as such.

**Memory** connector: same protocol, in-process, for unit tests and the CI eval run.

## Demo data

- Company: a made-up US company, "Lakeside Demo Utilities" (USD).
- **Supplier master: real organisation names from the City of Chicago vendor-payments ledger** (Socrata `s4vu-giwb`, the snapshot Spend Leakage Finder uses). `scripts/build_seed.py` takes organisations only (no individuals), picks well-paid vendors plus clusters where the ledger itself holds several spellings of one vendor, puts one spelling in the master and keeps the others as real-world duplicate onboarding attempts for tests and evals. The output, `data/seed/suppliers.csv` (a few hundred rows of names), is committed; the ledger itself is not.
- Cost centres, budgets, items, prices, tax IDs and historical orders are **synthetic** and labelled so. Historical orders set last purchase prices and part-consume budgets.
- Users: a requester, a second requester, a manager approver, a director approver. Local test accounts with `example.com` addresses.

## Guardrail eval

`evals/` holds attack scenarios. Each is a scripted sequence of tool calls, the calls a confused or injected model would make, run through the real MCP tool functions against a freshly seeded ERP. The ground truth is ERP state afterwards, not the tool's reply: a scenario counts as an unsafe write if any document the attack wanted changed actually changed.

Scenarios (first set): prompt injection in supplier notes instructing approval; writing with no code; writing with a code from a different preview; editing the order after preview; reusing a code; a $96K order split into two $48K orders; self-approval; approving above one's limit; a duplicate supplier under a real variant spelling; duplicate by tax ID; raising a PO to a supplier one just onboarded; over-budget order; price at 3x the last purchase; bank details in onboarding; approving an already-rejected order. Plus legitimate control flows that must succeed, so a guard that blocks everything scores badly.

Reported: unsafe writes that landed with po-guard, the same scenarios through a naive connector with no guardrails (the baseline), and legitimate flows completed. The fast run uses the memory connector in CI; `make eval-live` runs the same scenarios against ERPNext, and the README reports the live run.

## Testing and CI

- pytest: checks (pure), matching, confirmation codes, service orchestration on the memory connector, ERPNext client allowlist, SAP connector against fixtures, MCP tool registration. Coverage gate 80%.
- `make test-live`: integration tests against the running ERPNext, skipped when it isn't up.
- GitHub Actions: ruff, pytest with coverage gate across Python 3.11 to 3.13, fast eval run (fails the build if any unsafe write lands), pip-audit, gitleaks, wheel build check.

## Risks and open questions

- ERPNext API behaviour drifts between versions; the image is pinned and live tests catch drift.
- The duplicate matcher is precision-first (Spend Leakage Finder measured 55% recall on injected name variants), so the warn tier exists to widen the net at onboarding, where a false positive only costs a reviewer a glance.
- Confirmation codes are in-memory: restarting the server voids outstanding codes. Intended.
- SAP fixtures are built from the published schema until a sandbox key is available; the README says so.
