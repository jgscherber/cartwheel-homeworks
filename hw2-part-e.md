# HW2 Part E: PowerShell requests

Copy and paste each block into Windows PowerShell, one at a time, with the agent server and Langfuse stack running. These are the first five requests from `hw1-session.jsonl`, with their original user IDs, roles, and message text. Each block creates its own authenticated conversation and submits one message using `Invoke-WebRequest` (`iwr`).

These commands make live model calls. Request 3 can create a $240 refund queued for human approval; rerunning it can create additional records. The commands do not reset your database or print bearer tokens.

The server must have `TRACELOOP_TRACE_CONTENT=true` set before startup to capture message content. After each request, inspect its trace in Langfuse at `http://localhost:3000`. The `scenario_id` values below become `cartwheel.scenario_id` attributes to help identify the traces.

## 1. Shopper: check order 4127

```powershell
$sessionRequest = @{
    Uri = 'http://localhost:8010/sessions'
    Method = 'Post'
    ContentType = 'application/json'
    Body = (@{ user_id = 1; role = 'shopper' } | ConvertTo-Json)
    UseBasicParsing = $true
    ErrorAction = 'Stop'
}
$session = (Invoke-WebRequest @sessionRequest).Content | ConvertFrom-Json

$messageRequest = @{
    Uri = "http://localhost:8010/sessions/$($session.session_id)/messages"
    Method = 'Post'
    ContentType = 'application/json'
    Headers = @{ Authorization = "Bearer $($session.token)" }
    Body = (@{
        message = 'Show me the status of order 4127.'
        scenario_id = 'hw2-part-e-1'
    } | ConvertTo-Json)
    UseBasicParsing = $true
    ErrorAction = 'Stop'
}
$reply = (Invoke-WebRequest @messageRequest).Content | ConvertFrom-Json
$reply | Format-List
```

## 2. Shopper: request a refund for order 3980

```powershell
$sessionRequest = @{
    Uri = 'http://localhost:8010/sessions'
    Method = 'Post'
    ContentType = 'application/json'
    Body = (@{ user_id = 1; role = 'shopper' } | ConvertTo-Json)
    UseBasicParsing = $true
    ErrorAction = 'Stop'
}
$session = (Invoke-WebRequest @sessionRequest).Content | ConvertFrom-Json

$messageRequest = @{
    Uri = "http://localhost:8010/sessions/$($session.session_id)/messages"
    Method = 'Post'
    ContentType = 'application/json'
    Headers = @{ Authorization = "Bearer $($session.token)" }
    Body = (@{
        message = 'Please refund the full $52 for order 3980. I changed my mind.'
        scenario_id = 'hw2-part-e-2'
    } | ConvertTo-Json)
    UseBasicParsing = $true
    ErrorAction = 'Stop'
}
$reply = (Invoke-WebRequest @messageRequest).Content | ConvertFrom-Json
$reply | Format-List
```

## 3. Shopper: request a $240 refund for order 4455

This request can change the database by queuing a refund for human approval.

```powershell
$sessionRequest = @{
    Uri = 'http://localhost:8010/sessions'
    Method = 'Post'
    ContentType = 'application/json'
    Body = (@{ user_id = 1; role = 'shopper' } | ConvertTo-Json)
    UseBasicParsing = $true
    ErrorAction = 'Stop'
}
$session = (Invoke-WebRequest @sessionRequest).Content | ConvertFrom-Json

$messageRequest = @{
    Uri = "http://localhost:8010/sessions/$($session.session_id)/messages"
    Method = 'Post'
    ContentType = 'application/json'
    Headers = @{ Authorization = "Bearer $($session.token)" }
    Body = (@{
        message = 'Please refund the full $240 for order 4455. I received the wrong item.'
        scenario_id = 'hw2-part-e-3'
    } | ConvertTo-Json)
    UseBasicParsing = $true
    ErrorAction = 'Stop'
}
$reply = (Invoke-WebRequest @messageRequest).Content | ConvertFrom-Json
$reply | Format-List
```

## 4. Merchant: request an order outside their store

User 9002 is the merchant for store 2. Order 4127 belongs to store 1, so a `get_order` call should return a permission denial.

```powershell
$sessionRequest = @{
    Uri = 'http://localhost:8010/sessions'
    Method = 'Post'
    ContentType = 'application/json'
    Body = (@{ user_id = 9002; role = 'merchant' } | ConvertTo-Json)
    UseBasicParsing = $true
    ErrorAction = 'Stop'
}
$session = (Invoke-WebRequest @sessionRequest).Content | ConvertFrom-Json

$messageRequest = @{
    Uri = "http://localhost:8010/sessions/$($session.session_id)/messages"
    Method = 'Post'
    ContentType = 'application/json'
    Headers = @{ Authorization = "Bearer $($session.token)" }
    Body = (@{
        message = 'Show me the details of order 4127.'
        scenario_id = 'hw2-part-e-4'
    } | ConvertTo-Json)
    UseBasicParsing = $true
    ErrorAction = 'Stop'
}
$reply = (Invoke-WebRequest @messageRequest).Content | ConvertFrom-Json
$reply | Format-List
```

## 5. Support: check order 4127

The `Enter:` prefix is preserved from the original request in `hw1-session.jsonl`.

```powershell
$sessionRequest = @{
    Uri = 'http://localhost:8010/sessions'
    Method = 'Post'
    ContentType = 'application/json'
    Body = (@{ user_id = 9501; role = 'support' } | ConvertTo-Json)
    UseBasicParsing = $true
    ErrorAction = 'Stop'
}
$session = (Invoke-WebRequest @sessionRequest).Content | ConvertFrom-Json

$messageRequest = @{
    Uri = "http://localhost:8010/sessions/$($session.session_id)/messages"
    Method = 'Post'
    ContentType = 'application/json'
    Headers = @{ Authorization = "Bearer $($session.token)" }
    Body = (@{
        message = 'Enter: Show me the details of order 4127.'
        scenario_id = 'hw2-part-e-5'
    } | ConvertTo-Json)
    UseBasicParsing = $true
    ErrorAction = 'Stop'
}
$reply = (Invoke-WebRequest @messageRequest).Content | ConvertFrom-Json
$reply | Format-List
```
