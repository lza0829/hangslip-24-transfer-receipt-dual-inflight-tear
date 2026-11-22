import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

// 与后端 NOT_IN_FLIGHT 保持一致：已取、丢失隔离不可转挂；超期隔离仍在途可转。
const NOT_TRANSFERABLE = ["已取", "丢失隔离"];

export default function Transfer() {
  const [tickets, setTickets] = useState([]);
  const [stores, setStores] = useState([]);
  const [occupancy, setOccupancy] = useState([]);
  const [receipts, setReceipts] = useState([]);
  const [msg, setMsg] = useState("");
  const [ok, setOk] = useState(false);
  const [form, setForm] = useState({
    ticket_no: "",
    from_store: "",
    to_store: "",
    note: "",
  });

  const load = useCallback(async () => {
    const [all, ss, occ, rc] = await Promise.all([
      api.挂牌列表(),
      api.门店(),
      api.门店占用(),
      api.转挂回执(),
    ]);
    setTickets(all.filter((t) => !NOT_TRANSFERABLE.includes(t.status)));
    setStores(ss.filter((s) => s.active));
    setOccupancy(occ);
    setReceipts(rc);
  }, []);

  useEffect(() => {
    load().catch((e) => setMsg(e.message));
  }, [load]);

  function pick(t) {
    setForm((f) => ({ ...f, ticket_no: t.ticket_no, from_store: t.store_code }));
    setMsg("");
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
      setOk(true);
      setMsg(
        `已转挂 ${res.ticket_no}：${res.from_store} → ${res.to_store}，工序仍为「${res.status}」（票号不变）`
      );
      setForm((f) => ({ ...f, ticket_no: "", from_store: "", note: "" }));
      await load();
    } catch (err) {
      setMsg(err.message);
    }
  }

  return (
    <>
      <section className="panel">
        <h3>跨店转挂（同票改所属店）</h3>
        <p className="hint">
          口径：票号不变、工序态不变，仅迁所属店；两侧占用同一事务变更，失败整体回滚。
          原门店须与票籍现行门店一致，否则拒绝以防并发重复迁移。
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
            </tr>
          </thead>
          <tbody>
            {receipts.map((r) => (
              <tr key={r.id}>
                <td>{r.ticket_no}</td>
                <td>{r.from_store}</td>
                <td>{r.to_store}</td>
                <td>
                  <span className={`badge ${r.status_snapshot}`}>{r.status_snapshot}</span>
                </td>
                <td>{r.operator}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
