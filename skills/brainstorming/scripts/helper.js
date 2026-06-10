// Injected into every served page. Display-only: the sole job is reloading
// the browser when the agent writes a new or updated mockup.
(function() {
  const WS_URL = 'ws://' + window.location.host;

  function connect() {
    const ws = new WebSocket(WS_URL);

    ws.onmessage = (msg) => {
      const data = JSON.parse(msg.data);
      if (data.type === 'reload') {
        window.location.reload();
      }
    };

    ws.onclose = () => {
      setTimeout(connect, 1000);
    };
  }

  connect();
})();
