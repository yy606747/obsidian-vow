export default {
  async fetch(request) {
    const url = new URL(request.url);
    const target = new URL("https://generativelanguage.googleapis.com" + url.pathname + url.search);

    const headers = new Headers(request.headers);
    headers.delete("host");

    const resp = await fetch(target.toString(), {
      method: request.method,
      headers,
      body: request.method !== "GET" ? request.body : undefined,
    });

    const respHeaders = new Headers(resp.headers);
    respHeaders.set("Access-Control-Allow-Origin", "*");

    return new Response(resp.body, {
      status: resp.status,
      headers: respHeaders,
    });
  },
};
