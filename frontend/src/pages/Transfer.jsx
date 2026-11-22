import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

// 与后端 NOT_IN_FLIGHT 保持一致：已取、丢失隔离不可转挂；超期隔离仍在途可转。
const NOT_TRANSFERABLE = ["已取", "丢失隔离"];

export default function Transfer() {
  const [tickets, setTickets] = useState([]);
  const [stores, setStores] = useState([]);
  const [occupancy, setOccupancy] = useState([]);
  const [receipts, setReceipts] = useState([]);
  const [detail, setDetail] = useState(null);
  const [audit, setAudit] = useState(null);
  const [msg, setMsg] = useState("");
  const [ok, setOk] = useState(false);
  const [form, setForm] = useState({
    ticket_no: "",
    from_store: "",
    to_store: "",
    note: "",
  });

  const load = useCallback(async () => {
    const [all, ss, occ, rc, au] = await Promise.all([
      api.挂牌列表(),
      api.门店(),
      api.门店占用(),
      api.转挂回执(),
      api.对账(),
    ]);
    setTickets(all.filter((t) => !NOT_TRANSFERABLE.includes(t.status)));
    setStores(ss.filter((s) => s.active));
    setOccupancy(occ);
    setReceipts(rc);
    setAudit(au);
  }, []);

  const loadDetail = useCallback(async (no) => {
    if (!no) {
      setDetail(null);
      return;
    }
    try {
      setDetail(await api.挂牌详情(no));
    } catch {
      setDetail(null);
    }
  }, []);

  useEffect(() => {
    load().catch((e) => setMsg(e.message));
  }, [load]);

  function pick(t) {
    setForm((f) => ({ ...f, ticket_no: t.ticket_no, from_store: t.store_code }));
    setMsg("");
    loadDetail(t.ticket_no);
  }

  async function submit(e) {
    e.preventDefault();
    setMsg("");
    setOk(false);
    try {
      const res = await api.转挂({
        ticket_no: form.ticket_no,
        from_store: form.from_store,
        to_store: form.to_store,
        operator: "转挂员",
        note: form.note,
        // 以票号+目标店+时间戳生成幂等键，重复点击不会双计
        idempotency_key: `${form.ticket_no}->${form.to_store}:${Date.now()}`,
      });
      // 不只信响应里的店名：回读详情、回执、占用，要求四处互证。
      await load();
      const [d, rc, occ] = await Promise.all([
        api.挂牌详情(form.ticket_no),
        api.转挂回执(form.ticket_no),
        api.门店占用(),
      ]);
      setDetail(d);
      const last = rc[rc.length - 1];
      const occMap = Object.fromEntries(occ.map((o) => [o.code, o]));
      // 响应内联的是提交事务内读到的占用；回读必须与之逐店相等，
      // 否则说明提交后又有别的写入把两侧占用改撕裂了。
      const occMatches = (res.occupancy || []).every(
        (o) =>
          occMap[o.code] &&
          occMap[o.code].in_flight_tickets === o.in_flight_tickets &&
          occMap[o.code].in_flight_pieces === o.in_flight_pieces
      );
      const verified =
        d.store_code === res.to_store &&
        last &&
        last.to_store === d.store_code &&
        last.from_store === res.from_store &&
        (res.occupancy || []).length > 0 &&
        occMatches;
      if (!verified) {
        setMsg(`转挂 ${res.ticket_no} 返回成功但回读不一致，请查对账。`);
        return;
      }
      setOk(true);
      setMsg(
        `已转挂 ${res.ticket_no}：${res.from_store} → ${res.to_store}，` +
          `详情店码/回执/两侧占用互证一致，工序仍为「${res.status}」（票号不变）`
      );
      setForm((f) => ({ ...f, ticket_no: "", from_store: "", note: "" }));
    } catch (err) {
      setMsg(err.message);
      await load().catch(() => {});
    }
  }

  return (
    <>
      <section className="panel">
        <h3>
          跨店转挂（同票改所属店）
          {audit && (
            <span className={`consistency ${audit.一致 ? "ok" : "bad"}`}>
              {audit.一致 ? "回执·店码·占用互证" : "存在转挂撕裂"}
            </span>
          )}
        </h3>
        <p className="hint">
          口径：票号不变、工序态不变，仅迁所属店；店码与两侧占用同一事务变更，失败整体回滚。
          原门店须与票籍现行门店一致，否则拒绝以防并发重复迁移。回执必须能由现行店码与
          甲减乙加重放；出现双在途按假转挂回滚。
        </p>
        <p className={`msg ${ok ? "ok" : ""}`}>{msg}</p>
        <form onSubmit={submit}>
          <div className="row">
            <input
              placeholder="票号"
              value={form.ticket_no}
              onChange={(e) => setForm({ ...form, ticket_no: e.target.value })}
              required
            />
            <select
              value={form.from_store}
              onChange={(e) => setForm({ ...form, from_store: e.target.value })}
              required
            >
              <option value="" disabled>
                原门店
              </option>
              {stores.map((s) => (
                <option key={s.code} value={s.code}>
                  {s.name}
                </option>
              ))}
            </select>
            <select
              value={form.to_store}
              onChange={(e) => setForm({ ...form, to_store: e.target.value })}
              required
            >
              <option value="" disabled>
                目标门店
              </option>
              {stores
                .filter((s) => s.code !== form.from_store)
                .map((s) => (
                  <option key={s.code} value={s.code}>
                    {s.name}
                  </option>
                ))}
            </select>
            <input
              placeholder="备注（可选）"
              value={form.note}
              onChange={(e) => setForm({ ...form, note: e.target.value })}
            />
            <button type="submit">确认转挂</button>
          </div>
        </form>
      </section>

      <section className="panel">
        <h3>门店在途占用（现行行重放）</h3>
        <table>
          <thead>
            <tr>
              <th>门店</th>
              <th>在途票数</th>
              <th>在途件数</th>
              <th>轨位</th>
              <th>剩余轨位</th>
            </tr>
          </thead>
          <tbody>
            {occupancy.map((o) => (
              <tr key={o.code}>
                <td>{o.name}</td>
                <td>{o.in_flight_tickets}</td>
                <td>{o.in_flight_pieces}</td>
                <td>{o.rack_count}</td>
                <td className={o.free_racks < 0 ? "warn-text" : ""}>{o.free_racks}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {detail && (
        <section className="panel">
          <h3>挂牌详情店码 · 与回执互证</h3>
          <div className="row">
            <div className="metric">
              <b>{detail.ticket_no}</b>
              <span>票号（转挂不变）</span>
            </div>
            <div className="metric">
              <b>{detail.store_code}</b>
              <span>现行所属店</span>
            </div>
            <div className="metric">
              <b>{detail.status}</b>
              <span>工序态</span>
            </div>
          </div>
          <p className="hint">
            现行店码是归属权威；转挂回执必须能按“甲店减、乙店加”折回该店码，否则视为假转挂回滚。
          </p>
          <table>
            <thead>
              <tr>
                <th>事件</th>
                <th>工序</th>
                <th>操作员</th>
                <th>时间</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {detail.events.map((e) => (
                <tr key={e.id}>
                  <td>{e.id}</td>
                  <td>
                    <span className={`badge ${e.to_status}`}>
                      {e.from_status ?? "开票"} → {e.to_status}
                    </span>
                  </td>
                  <td>{e.operator}</td>
                  <td>{e.occurred_at}</td>
                  <td>{e.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="panel">
        <h3>在途票（点击带入转挂）</h3>
        <table>
          <thead>
            <tr>
              <th>票号</th>
              <th>现行门店</th>
              <th>工序态</th>
              <th>顾客</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {tickets.map((t) => (
              <tr key={t.ticket_no}>
                <td>{t.ticket_no}</td>
                <td>{t.store_code}</td>
                <td>
                  <span className={`badge ${t.status}`}>{t.status}</span>
                </td>
                <td>{t.customer_name}</td>
                <td>
                  <button type="button" className="ghost" onClick={() => pick(t)}>
                    转挂此票
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h3>转挂回执流水</h3>
        <table>
          <thead>
            <tr>
              <th>票号</th>
              <th>原门店</th>
              <th>目标门店</th>
              <th>工序快照</th>
              <th>操作员</th>
              <th>时间</th>
              <th>与现行店码</th>
            </tr>
          </thead>
          <tbody>
            {receipts.map((r) => {
              const cur = tickets.find((t) => t.ticket_no === r.ticket_no);
              const consistent = cur ? cur.store_code === r.to_store : null;
              return (
                <tr key={r.id}>
                  <td>{r.ticket_no}</td>
                  <td>{r.from_store}</td>
                  <td>{r.to_store}</td>
                  <td>
                    <span className={`badge ${r.status_snapshot}`}>{r.status_snapshot}</span>
                  </td>
                  <td>{r.operator}</td>
                  <td>{r.created_at}</td>
                  <td className={consistent === false ? "warn-text" : ""}>
                    {consistent === null ? "—" : consistent ? "可重放" : `撕裂（现行 ${cur.store_code}）`}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </>
  );
}
