
# Python Engineering Standards for Codex

This steering file instructs the Codex agent how to generate Python code following:

- Domain Driven Design principles
- solid Low Level Design
- explicit API contracts
- strong persistence and consistency models
- GoF patterns applied idiomatically in Python
- clean code and maintainable architecture

The goal is to produce production-grade code that is readable, deterministic, testable, and maintainable.

Avoid clever or magical code. Prefer explicitness and clarity.

For this repository specifically, complex infrastructure code such as kernel
probing, syscall wrappers, bootstrap wiring, orchestration, and security setup
MUST receive extra documentation effort. When editing those areas, prefer
longer, didactic explanations over terse code.

---

# Core Philosophy

The codebase must prioritize:

clarity > cleverness  
correctness > premature optimization  
explicit behavior > implicit magic  
composition > inheritance  
determinism > hidden side effects

Design code as if another engineer must maintain it for the next 10 years.

All important business logic must be understandable by reading the code.

---

# Kiro Behavior Rules

When generating Python code, the Kiro agent must:

1. prefer readable and maintainable code
2. generate descriptive names
3. include explanatory comments
4. avoid duplicated logic
5. create helper classes when necessary
6. keep functions small
7. respect domain boundaries
8. implement DDD concepts when applicable
9. follow Python idioms
10. avoid unnecessary complexity

The resulting code should resemble what a human senior Python engineer would write in a production system.

---

# Naming Conventions

Always prefer **human readable names**.

Avoid cryptic abbreviations.

Bad:

```

def calc(x,y):

```

Good:

```

def calculate_account_balance(transactions, exchange_rates):

```

Classes must describe **what they represent**.

Examples:

```

AccountRepository
PaymentAuthorizationService
CurrencyConversionPolicy
LedgerEntry
OrderAggregate
UserRegistrationUseCase

```

Variables should describe the concept they hold.

Bad:

```

tmp
obj
data

```

Good:

```

customer_account
transaction_record
exchange_rate_snapshot
pending_payment_request

```

---

# Comments

This project **prefers abundant and didatic explanatory comments**.

Comments must explain:

- intent
- invariants
- domain rules
- non-obvious decisions
- algorithm constraints

Avoid obvious comments.

Bad:

```

# increment i

i += 1

```

Good:

```

# increment retry counter to enforce exponential backoff policy

retry_attempt += 1

```

Complex functions must begin with a **short didatic explanation block**.

For this repository, prefer **Google-style docstrings** for important modules,
classes, and methods.

These docstrings and comments should, when relevant, explain:

- which requirement(s) from `specs/run_spec/requirements.md` the code is implementing
- which section or concept from `specs/run_spec/design.md` the code is following
- the step-by-step flow of the algorithm
- the invariants being protected
- why a specific implementation decision was chosen

For complex orchestration code, do not stop at a one-line summary. Add a short
didactic block that helps a human reader understand the full execution flow.

For kernel, syscall, namespace, cgroup, seccomp, mount, and other low-level
runtime code, comments should explicitly explain:

- what is being probed or configured
- why the operation is safe
- why a specific errno/result implies feature support or absence
- what the step means in terms of the container runtime design

If the code is hard to follow without Linux internals knowledge, it is
under-documented.

When generating new code or editing existing code, prefer:

- module docstrings with architectural context
- class docstrings with responsibility and invariants
- method docstrings with `Args:`, `Returns:`, and `Raises:` when applicable
- short inline comments before non-obvious blocks

The goal is that a human should be able to read the code and understand:

- what it does
- why it exists
- how it connects to the spec
- what happens step by step

Example:

```

def reconcile_external_payments(...):
"""
Reconciliation algorithm.

```
Steps:
1. load internal ledger entries
2. match with external provider settlement
3. detect discrepancies
4. emit reconciliation events

Invariant:
ledger must remain append-only.
"""
```

```

---

# Python Style Guidelines

Prefer Python idioms.

Use:

- dataclasses
- type hints
- protocols
- composition
- pure functions where possible
- immutability when feasible

Avoid:

- deep inheritance trees
- unnecessary abstract base classes
- global state
- mutable shared structures

Example:

Preferred:

```

@dataclass(frozen=True)
class Money:
   amount: Decimal
   currency: str

```

---

# Function Design

Functions must be:

small  
predictable  
single responsibility  

Target size:

5-30 lines.

If a function grows too large:

split logic into **private helper methods**.

Preferred structure:

```
class PaymentProcessor:
   def process_payment(self, request: PaymentRequest) -> PaymentResult:
      validated_request = self._validate_request(request)
      authorization = self._authorize(validated_request)
      ledger_entry = self._record_transaction(authorization)
      return self._build_response(ledger_entry)

   def _validate_request(self, request):
      ...

   def _authorize(self, request):
      ...

   def _record_transaction(self, authorization):
      ...
```

Prefer **private helper methods** over standalone functions.

---

# Avoid Code Duplication

When the same logic appears in multiple places:

extract a **shared helper**.

Example:

```

class PaginationHelper:

```
@staticmethod
def build_page_metadata(page, page_size, total):
    ...
```

```

Shared helpers must live in dedicated modules.

Examples:

```

utils/
shared/
infrastructure/helpers/

```

Never copy paste business logic.

---

# Domain Driven Design

Use DDD concepts where business rules exist.

Core elements:

Entity  
Value Object  
Aggregate  
Domain Service  
Repository  
Factory  
Domain Event  

Example entity:

```

class Account:
   def deposit(self, amount: Money):
      ...

```

Entities must contain behavior.

Avoid anemic models.

---

# Aggregates

Aggregates protect invariants.

External code must modify aggregates only through **public methods**.

Example:

```

class OrderAggregate:
   def add_item(self, product, quantity):
      ...

   def confirm(self):
      ...

```

Never mutate aggregate state directly.

---

# Value Objects

Value objects must be:

immutable  
comparable  
side effect free

Example:

```

@dataclass(frozen=True)
class CurrencyCode:
   value: str

```

---

# Repositories

Repositories abstract persistence.

They should expose **domain language**, not SQL language.

Good:

```

account_repository.find_by_id(account_id)

```

Bad:

```

account_repository.execute_query(...)

```

Repositories should not contain domain logic.

---

# API Design

APIs must define explicit contracts.

Follow these principles:

clear input schema  
clear output schema  
structured errors  
stable pagination  
idempotent operations where applicable  

Example response:

```

{
   "status": "success",
   "data": {...},
   "metadata": {...}
}

```

Errors must be structured.

```

{
   "error": {
   "code": "ACCOUNT_NOT_FOUND",
      "message": "Account does not exist"
   }
}

```

---

# Validation Layers

Always validate data in layers.

1 API layer  
2 application layer  
3 domain layer  

Each layer enforces its own invariants.

---

# Persistence and Consistency

Persistence must clearly define:

source of truth  
transaction boundaries  
consistency guarantees  

Avoid hidden writes.

Use explicit transactions when required.

Example:

```

with transaction_manager.start():
   repository.save(entity)

```

---

# Concurrency

When concurrent writes are possible:

use one of the following:

optimistic locking  
version numbers  
idempotency keys  

Example:

```

version: int

```

---

# Events

Use domain events for decoupling.

Example:

```

AccountCreated
PaymentAuthorized
InvoiceSettled

```

Events must represent facts that happened and be well documented.

---

# GoF Patterns (Python Adaptation)

Use patterns only when they simplify design.

Factory:

```

class PaymentGatewayFactory:

```
def create_gateway(self, provider):
    ...
```

```

Strategy:

```

class PricingStrategy(Protocol):

```
def calculate(self, order):
    ...
```

```

Avoid Singleton.

Modules already behave as singletons in Python.

---

# Logging

Logs must describe events, not debug noise.

Include:

entity id  
operation  
result  

Example:

```

logger.info(
   "payment authorized",
   payment_id=payment.id,
   amount=payment.amount
)

```

---

# Testing Expectations

Generated code must be testable.

Prefer dependency injection.

Avoid hardcoded infrastructure.

Example:

```

class PaymentService:

   def __init__(self, repository, gateway):
      ...

```

---

# Documentation

Important modules must contain:

module level docstrings  
domain explanations  
examples when helpful  

---

# Codex Behavior Rules

When generating Python code, the Kiro agent must:

1. prefer readable and maintainable code
2. generate descriptive names
3. include explanatory comments
4. avoid duplicated logic
5. create helper classes when necessary
6. keep functions small
7. respect domain boundaries
8. implement DDD concepts when applicable
9. follow Python idioms
10. avoid unnecessary complexity

The resulting code should resemble what a human senior Python engineer would write in a production system.

THE SAME APPLIES TO C++ AND CYTHON CODE

