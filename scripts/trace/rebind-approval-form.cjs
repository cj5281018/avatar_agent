/**
 * Rebinding a supervisor (A -> B) must reject an approval form opened under A,
 * leave the request pending, and restore A's rights after renewing.
 */
const { chromium } = require('playwright-core')
const assert = require('node:assert/strict')
const { execFileSync } = require('node:child_process')

const base = process.env.BASE || 'http://localhost:8301'
const hrBase = process.env.HR_BASE || 'http://localhost:8892'
const modelId = process.env.MODEL_ID

const dockerPy = (code) => execFileSync('docker', ['exec', '-i', 'avatar2-source-gateway-1', 'python', '-'], { input: code, encoding: 'utf8' }).trim()
const psql = (sql) => execFileSync('docker', ['exec', '-i', 'avatar2-source-postgres-1', 'sh', '-c', 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At'], { input: sql, encoding: 'utf8' }).trim()

async function login(page, email, password) {
    await page.goto(base + '/login')
    await page.locator('[name=email]').fill(email)
    await page.locator('[name=password]').fill(password)
    await page.locator('button[type=submit]').click()
    await page.waitForURL((url) => url.pathname !== '/login')
}

async function renew(page) {
    await page.goto(base + '/employee-binding')
    await page.getByRole('button', { name: '绑定或续期' }).click()
    await page.getByText(/已绑定，有效至/).waitFor({ timeout: 15000 })
    await page.waitForTimeout(300)
}

async function ticket(page, target) {
    const response = await page.request.post(base + '/api/hr-entry', {
        data: { target },
        headers: { origin: base },
    })
    assert(response.ok(), 'ticket ' + response.status())
    return response.json()
}

;(async () => {
    const bobId = psql("select id from users where email='bob@avatar2.test'")
    const adminId = psql("select id from users where email='admin@avatar2.test'")
    // Ensure Bob has a binding, then create a pending request assigned to admin.
    const bobBind = execFileSync('powershell', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        'C:\\Users\\陈健\\AppData\\Local\\Temp\\avatar2-browser-test\\run-bob-script.ps1',
        'C:/Users/陈健/AppData/Local/Temp/avatar2-browser-test/bind-bob.cjs'], { encoding: 'utf8' })
    void bobBind
    const confirmed = JSON.parse(dockerPy(`import json,os,urllib.request
def call(payload):
    req=urllib.request.Request("http://127.0.0.1:8790/internal/tools/call", data=json.dumps(payload).encode(), headers={"Authorization":"Bearer "+os.environ["AVATAR2_INTERNAL_TOKEN"],"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(req,timeout=15).read())
print(json.dumps(call({"user_id":"${bobId}","chat_id":"01M00000000000000000000000","name":"hr_preview_leave","arguments":{"startDate":"2027-08-01","endDate":"2027-08-01","leaveType":"annual","reason":"rebind form"}})))`))
    const op = confirmed.operationId
    const card = JSON.parse(dockerPy(`import json,os,urllib.request
def call(payload):
    req=urllib.request.Request("http://127.0.0.1:8790/internal/cards/01M00000000000000000000000/${op}/confirm", data=json.dumps(payload).encode(), headers={"Authorization":"Bearer "+os.environ["AVATAR2_INTERNAL_TOKEN"],"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(req,timeout=15).read())
print(json.dumps(call({"user_id":"${bobId}"})))`))
    const leaveId = card.leaveId
    assert(leaveId, 'pending leave expected: ' + JSON.stringify(card))

    const browser = await chromium.launch({ channel: 'msedge', headless: true })
    try {
        const adminContext = await browser.newContext()
        const admin = await adminContext.newPage()
        await login(admin, process.env.ADMIN_EMAIL, process.env.ADMIN_PASSWORD)
        await renew(admin)
        const hr = await adminContext.newPage()
        const t = await ticket(admin, 'approvals')
        await hr.request.post(t.action, { form: { ticket: t.ticket } })
        await hr.goto(hrBase + '/web/approvals')
        await hr.locator('article').filter({ hasText: leaveId }).waitFor({ timeout: 20000 })
        const csrf = await hr.locator('meta[name=hr-csrf]').getAttribute('content')

        // Rebind the admin workbench account to another HR identity (carol).
        dockerPy(`import os,secrets,sqlite3,hashlib,urllib.request,json
from avatar_service.db import now
db=sqlite3.connect('/data/avatar.sqlite3')
row=db.execute("select id from users where handle='carol'").fetchone()
if row is None:
    uid='usr_carol_fixture'
    db.execute("INSERT OR IGNORE INTO users VALUES (?,?,?,?,?,?)",(uid,'carol','Carol','member','s','h'))
    row=(uid,)
token=secrets.token_urlsafe(32)
db.execute("INSERT INTO logins VALUES (?,?,?)",(hashlib.sha256(token.encode()).hexdigest(),row[0],now()+3600000)); db.commit()
req=urllib.request.Request("http://127.0.0.1:8790/internal/bindings", data=json.dumps({"user_id":"${adminId}","token":token}).encode(), headers={"Authorization":"Bearer "+os.environ["AVATAR2_INTERNAL_TOKEN"],"Content-Type":"application/json"})
print(urllib.request.urlopen(req,timeout=15).status)`)

        const oldForm = await hr.request.post(`${hrBase}/web/approvals/${leaveId}/approve`, {
            form: { csrf: csrf ?? '' },
            maxRedirects: 0,
        })
        const oldText = await oldForm.text()
        assert(
            [409, 403].includes(oldForm.status()) ||
                /重新进入|绑定已变更|无法访问|无权|身份/.test(oldText),
            'old form was not rejected: ' + oldForm.status(),
        )
        const still = execFileSync('docker', ['exec', 'avatar2-source-hr-1', 'python', '-c',
            `import sqlite3,glob; db=sqlite3.connect(glob.glob('/var/lib/hr/*.sqlite3')[0]); print(db.execute("select status from leave_requests where id=?", ("${leaveId}",)).fetchone()[0])`], { encoding: 'utf8' }).trim()
        assert.equal(still, 'pending', 'request must stay pending after rejected rebind form')
        console.log('PASS supervisor rebind rejected the old approval form; request still pending')

        // Renew restores the directory identity (admin) and approval succeeds.
        await renew(admin)
        const hr2 = await adminContext.newPage()
        const t2 = await ticket(admin, 'approvals')
        await hr2.request.post(t2.action, { form: { ticket: t2.ticket } })
        await hr2.goto(hrBase + '/web/approvals')
        await hr2.locator('article').filter({ hasText: leaveId }).getByRole('button', { name: '批准' }).click({ timeout: 20000 })
        await hr2.waitForURL('**/web/approvals')
        const approved = execFileSync('docker', ['exec', 'avatar2-source-hr-1', 'python', '-c',
            `import sqlite3,glob; db=sqlite3.connect(glob.glob('/var/lib/hr/*.sqlite3')[0]); print(db.execute("select status from leave_requests where id=?", ("${leaveId}",)).fetchone()[0])`], { encoding: 'utf8' }).trim()
        assert.equal(approved, 'approved')
        console.log('PASS renewal restored the supervisor identity and approval succeeded')
    } finally {
        await browser.close()
    }
})().catch((error) => {
    console.error('FAIL', error.message)
    process.exitCode = 1
})
