// Run after npm run build. Uses isolated Chrome and synthetic data, no live login.
// CSP_FILE points to the policy exported from Scripts/harden_site.py.
const {chromium} = require('playwright-core');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const host = 'https://regattaview.net';
const lambda = 'https://blsywilg2fba6564nixqxwntyy0iagoj.lambda-url.us-west-2.on.aws/';
(async () => {
    const browser = await chromium.launch({channel:'chrome',headless:true});
    const context = await browser.newContext();
    const csp = await fs.readFile(process.env.CSP_FILE, 'utf8');
    const errors = [], violations = [];
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.stack || error.message));
    await context.addInitScript(() => {
        localStorage.setItem('regatta_session_token', 'synthetic-test-only');
        window.cspViolations = [];
        document.addEventListener('securitypolicyviolation', e => window.cspViolations.push({directive:e.violatedDirective,blocked:e.blockedURI,source:e.sourceFile,line:e.lineNumber,column:e.columnNumber}));
    });
    const names = ['row_seconds','latitude','longitude','spd','sog','twa','awa','tws','aws','hdg','cog','zg100_roll','rudder'];
    const columns = Object.fromEntries(names.map((name,i)=>[name,i]));
    const rows = 100, binary = Buffer.alloc(16 + rows * names.length * 8);
    binary.writeBigUInt64LE(BigInt(rows),0);binary.writeBigUInt64LE(BigInt(names.length),8);
    names.forEach((name,col)=>{for(let row=0;row<rows;row++) {
        const value = name==='row_seconds'?row:name==='latitude'?47.65+row/1e5:name==='longitude'?-122.4+row/1e5:5+row/100;
        binary.writeDoubleLE(value,16+(col*rows+row)*8);
    }});
    let redirectOnly = true, revision = null, document = null, lastWrite;
    await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (request.url() === lambda) {
            const headers = {'access-control-allow-origin':host,'access-control-allow-headers':'content-type','access-control-allow-methods':'POST, OPTIONS'};
            if(request.method()==='OPTIONS')return route.fulfill({status:204,headers});
            const body=request.postDataJSON();let data={};let status=200;
            if(body.method==='GET')data={url:host+'/'+body.filename};
            if(body.method==='READ_JSON')data={data:document,etag:revision};
            if(body.method==='SAVE_JSON') {
                if(body.etag!==revision){status=409;data={message:'Changed by another editor. Reload before saving.'};}
                else {document=body.data;revision='"v'+Date.now()+'"';data={etag:revision};lastWrite=body;}
            }
            return route.fulfill({status,headers,contentType:'application/json',body:JSON.stringify(data)});
        }
        if(url.origin!==host)return route.continue();
        if(url.pathname.startsWith('/test-modules/')) {
            const name=path.basename(url.pathname).replace(/\.js$/,'');
            if(!['shared','utils','security','auth_config'].includes(name))return route.abort();
            return route.fulfill({contentType:'text/javascript',body:await fs.readFile(path.join(root,'src/js',name+'.js'))});
        }
        if(url.pathname==='/test.html'||redirectOnly&&url.pathname!=='/login.html'&&url.pathname.endsWith('.html')) {
            return route.fulfill({contentType:'text/html',headers:{'content-security-policy':csp},body:'<!doctype html><title>Test destination</title><div id="label"></div>'});
        }
        if(url.pathname==='/www/Logs/video_poses_logs.json')return route.fulfill({json:{logs:[],columns}});
        if(url.pathname.startsWith('/www/FullLogs/'))return route.fulfill({contentType:'application/octet-stream',body:binary});
        if(url.pathname.startsWith('/www/SeattleTiles/'))return route.fulfill({contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=','base64')});
        try {
            const body=await fs.readFile(path.join(root,'dist',url.pathname));
            const mime={'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.json':'application/json','.woff2':'font/woff2'};
            return route.fulfill({body,contentType:mime[path.extname(url.pathname)]||'application/octet-stream',headers:{'content-security-policy':csp}});
        } catch {return route.fulfill({status:404,body:''});}
    });
    try {
        for(const future of ['javascript:window.__xss=1','https://evil.invalid/review.html','/review.html?day=2026-10-19&tab=comments']) {
            await page.goto(host+'/login.html?future='+encodeURIComponent(future));
            const expected=future.startsWith('/review')?host+future:host+'/sailview.html';
            await page.waitForURL(expected);
            assert.equal(await page.evaluate(()=>window.__xss),undefined);
        }
        console.log('PASS production login bundle: dangerous destinations blocked; deep link preserved');
        await page.goto(host+'/test.html');
        const result=await page.evaluate(async()=>{
            const security=await import('/test-modules/security');
            const el=document.getElementById('label');
            security.renderPlaylistLabel(el,'<img src=x onerror=window.__xss=1>','1/1',{
                type:'<svg onload=window.__xss=1>',name:'<script>window.__xss=1</script>',start:1,end:2,comment:'<img src=x onerror=window.__xss=1>'},String);
            return {html:el.querySelectorAll('img,svg,script').length,text:el.textContent,xss:window.__xss};
        });
        assert.equal(result.html,0);assert.equal(result.xss,undefined);assert.match(result.text,/<img/);
        console.log('PASS playlist labels render untrusted content as text');
        const saves=await page.evaluate(async()=>{
            const shared=await import('/test-modules/shared');
            const save=data=>new Promise(resolve=>shared.putDataJSON('Analyze/events_edits.json',data,error=>resolve(error||null)));
            const before=await save({version:1,edits:{}});
            await shared.readDataJSON('Analyze/events_edits.json');
            const accepted=await save({version:1,edits:{race:{status:'confirmed'}}});
            return {before,accepted};
        });
        assert.match(saves.before,/Reload/);assert.equal(saves.accepted,null);assert.equal(lastWrite.data.edits.race.status,'confirmed');
        revision='"another-editor"';
        const conflict=await page.evaluate(async()=>{
            const shared=await import('/test-modules/shared');
            return new Promise(resolve=>shared.putDataJSON('Analyze/events_edits.json',{version:1,edits:{}},resolve));
        });
        assert.match(conflict,/another editor/);
        console.log('PASS browser document API: read-before-write and conflict errors');
        redirectOnly=false;
        await page.goto(host+'/graphs.html');
        try {
            await page.waitForFunction(()=>Boolean(window.stuff?.pv),null,{timeout:20000});
        } catch (error) {
            console.error({errors,csp:await page.evaluate(()=>window.cspViolations),state:await page.evaluate(()=>Object.keys(window.stuff||{}))});
            throw error;
        }
        assert.equal(await page.locator('#graphs canvas').count()>0,true);
        violations.push(...await page.evaluate(()=>window.cspViolations));
        assert.deepEqual(errors,[]);assert.deepEqual(violations,[]);
        console.log('PASS production graph renders synthetic instrument data under enforced CSP');
    } finally {await context.close();await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
