// Прокси перед api.telegram.org (см. STATE.md - api.telegram.org заблокирован
// для российских хостингов, поэтому бот ходит в Telegram через этот воркер).
//
// TG_BOT_TOKEN - секрет Cloudflare Worker (Secret, не переменная в открытом
// виде), проверяется тут же: без него любой, кто найдёт URL воркера, мог бы
// гонять чужой трафик к любому Telegram-боту за счёт квоты Cloudflare Workers
// владельца воркера. Значение секрета никогда не хранится в этом файле и не
// попадает в git - оно задаётся отдельно через Cloudflare API (secrets) или
// дашборд Cloudflare.
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const allowedPrefix = "/bot" + env.TG_BOT_TOKEN;
    if (!url.pathname.startsWith(allowedPrefix)) {
      return new Response("Forbidden", { status: 403 });
    }
    const targetUrl = "https://api.telegram.org" + url.pathname + url.search;
    return fetch(new Request(targetUrl, request));
  }
};
