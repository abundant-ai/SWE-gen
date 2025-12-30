Axios does not use Node’s HTTP/2 transport, so requests to HTTP/2 endpoints either fail or fall back to HTTP/1.1 and cannot take advantage of HTTP/2 features (multiplexing, improved performance). Reproduce by running an HTTP/2 server and performing a standard GET with Axios in Node.js (v9+):

const axios = require('axios');
axios.get('https://localhost:8443').then(r => console.log(r.status)).catch(e => console.error(e));

Expected behavior: the request should establish an HTTP/2 connection to the server and complete successfully using HTTP/2 semantics instead of failing or downgrading to HTTP/1.1.