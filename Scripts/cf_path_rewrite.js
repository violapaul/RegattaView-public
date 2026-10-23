// CloudFront Function — viewer-request handler that maps clean URLs onto
// the S3 layout. The bucket holds the HTML/JS shell at /www/sailview/* (so
// `regattaanalysis/auth/*` and other internal prefixes don't collide with
// public assets). Without this rewrite, viewers had to type the raw key:
//   https://regattaview.net/www/sailview/sailview.html
//
// Wiring (one-time):
//   aws cloudfront create-function \
//     --name regattaview-path-rewrite \
//     --function-config 'Comment="Map clean URLs to /www/sailview/*",Runtime=cloudfront-js-2.0' \
//     --function-code fileb://Scripts/cf_path_rewrite.js
//   aws cloudfront publish-function --name regattaview-path-rewrite --if-match <ETag>
//   # Then attach as ViewerRequest on the default cache behavior of EPCTF7XCNPLK2.
//
// Update procedure:
//   aws cloudfront describe-function --name regattaview-path-rewrite --stage DEVELOPMENT \
//     --query 'ETag' --output text
//   aws cloudfront update-function --name regattaview-path-rewrite \
//     --if-match <ETag> --function-config '...same as above...' \
//     --function-code fileb://Scripts/cf_path_rewrite.js
//   aws cloudfront publish-function --name regattaview-path-rewrite --if-match <new ETag>
//
// Constraints (cloudfront-js-2.0):
//   - 10 KB max compiled size
//   - 1 ms compute budget
//   - No top-level await, no fetch, no setTimeout
//   - `event.request.uri` is the path component only (no scheme/host/query)
//   - Returning the request unmodified passes through to the origin

function handler(event) {
    var request = event.request;
    var uri = request.uri;

    // Default landing → the main viewer page.
    if (uri === '/' || uri === '') {
        request.uri = '/www/sailview/sailview.html';
        return request;
    }

    // /www/* paths are managed by their own cache behaviors (signed data
    // plane at /www/Insta360/*, /www/Logs/*, /www/Analyze/*, etc., and
    // the public HTML shell at /www/sailview/*). Leave them alone — this
    // function is only attached to the default behavior, but be defensive
    // in case it's ever attached more broadly.
    if (uri.indexOf('/www/') === 0) {
        return request;
    }

    // Everything else is a top-level path that should resolve under the
    // HTML shell:
    //   /sailview.html  -> /www/sailview/sailview.html
    //   /review.html    -> /www/sailview/review.html
    //   /js/x_pack.js   -> /www/sailview/js/x_pack.js  (relative-link fallout)
    //   /css/x.css      -> /www/sailview/css/x.css
    //   /assets/foo.svg -> /www/sailview/assets/foo.svg
    request.uri = '/www/sailview' + uri;
    return request;
}
