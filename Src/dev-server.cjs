// Loopback-only development server. No public proxy, directory listing, or HMR.
const http = require('node:http');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const webpack = require('webpack');
const config = require('./webpack.config');
const port = Number(process.env.PORT || 9000);
const analyze = path.resolve(process.env.REGATTA_ANALYZE_DIR || path.join(__dirname, '..', 'Analyze'));
const localData = path.join(__dirname, '.local-data');
const mime = {'.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.json':'application/json', '.svg':'image/svg+xml', '.png':'image/png', '.jpg':'image/jpeg', '.woff':'font/woff', '.woff2':'font/woff2', '.map':'application/json'};

function targetFor(url) {
    if (url.startsWith('/Comments/')) return [path.join(analyze,'Comments'),url.slice(10)];
    if (url.startsWith('/Insta360/')) return [path.join(localData,'Insta360'),url.slice(10)];
    if (url.startsWith('/SeattleTiles/')) return [process.env.REGATTA_TILES_DIR || '/Users/viola/www/SeattleTiles',url.slice(14)];
    if (url.startsWith('/Analyze/')) {
        const name=url.slice(9);
        if (!/^(events_(index|edits)\.json|(?:Events|Sessions|AIS|AISDays|Transcripts|Notes)\/)/.test(name) || /(^|\/)scripts\//.test(name)) return null;
        return [analyze,name];
    }
    return [path.join(__dirname,'dist'), url === '/' ? 'sailview.html' : url.slice(1)];
}

async function handler(req,res) {
    try {
        const allowedHosts=new Set([`localhost:${port}`,`127.0.0.1:${port}`]);
        if (!allowedHosts.has(req.headers.host)) {res.writeHead(403);res.end();return;}
        if (req.headers.origin && ![`http://localhost:${port}`,`http://127.0.0.1:${port}`].includes(req.headers.origin)) {res.writeHead(403);res.end();return;}
        const url=decodeURIComponent(new URL(req.url,`http://localhost:${port}`).pathname);
        if (url.includes('\\') || url.includes('\0') || url.split('/').some(x=>x.startsWith('.'))) {res.writeHead(403);res.end();return;}
        const target=targetFor(url);
        if (!target) {res.writeHead(404);res.end();return;}
        const [root,relative]=target;
        const file=path.resolve(root,relative);
        if (!file.startsWith(path.resolve(root)+path.sep)) {res.writeHead(403);res.end();return;}
        res.setHeader('Cache-Control','no-store');res.setHeader('X-Content-Type-Options','nosniff');
        if (req.method === 'PUT') {
            if (!/^\/(?:Analyze\/events_edits\.json|Insta360\/curated\.json|Comments\/(?:index\.json|[A-Za-z0-9_-]{1,30}\/comments\.json))$/.test(url)) {res.writeHead(403);res.end();return;}
            const chunks=[];let size=0;
            for await (const chunk of req) {size+=chunk.length;if(size>1024*1024){res.writeHead(413);res.end();return;}chunks.push(chunk);}
            const raw=Buffer.concat(chunks);const data=JSON.parse(raw);
            if(!data || Array.isArray(data) || typeof data!=='object'){res.writeHead(400);res.end();return;}
            await fsp.mkdir(path.dirname(file),{recursive:true});
            const temporary=file+'.'+crypto.randomUUID()+'.tmp';
            await fsp.writeFile(temporary,raw);await fsp.rename(temporary,file);
            res.writeHead(200,{'Content-Type':'application/json'});res.end('{"ok":true}');return;
        }
        if(!['GET','HEAD'].includes(req.method)){res.writeHead(405);res.end();return;}
        const stat=await fsp.stat(file);if(!stat.isFile()){res.writeHead(404);res.end();return;}
        res.writeHead(200,{'Content-Type':mime[path.extname(file)] || 'application/octet-stream','Content-Length':stat.size});
        if(req.method==='HEAD'){res.end();return;}
        const stream=fs.createReadStream(file);stream.on('error',()=>res.destroy());stream.pipe(res);
    } catch(error) {if(!res.headersSent)res.writeHead(error.code==='ENOENT'?404:400);res.end();}
}

const server=http.createServer(handler);
server.requestTimeout=15000;server.headersTimeout=10000;
let watcher;
if(process.env.REGATTA_SKIP_BUILD!=='1') {
    // Real source maps rather than eval-based bundles; mirrors production CSP.
    watcher=webpack({...config,mode:'development'}).watch({},(error,stats)=>{
        if(error)console.error(error.message);else console.log(stats.toString({all:false,errors:true,warnings:true,timings:true}));
    });
}
server.listen(port,'127.0.0.1',()=>console.log(`RegattaView: http://localhost:${port}/sailview.html (refresh after rebuild)`));
function close(){server.close();if(watcher)watcher.close(()=>process.exit());else process.exit();}
process.on('SIGINT',close);process.on('SIGTERM',close);
