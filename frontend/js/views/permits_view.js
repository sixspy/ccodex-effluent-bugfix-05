export const permitsViewTemplate = `
  <div class="grid-2">
    <div class="panel">
      <h3>排放许可册</h3>
      <table>
        <thead><tr><th>许可号</th><th>厂</th><th>配额</th><th>COD帽</th><th>期别</th><th>有效期至</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="p in permits" :key="p.id">
            <td>{{ p.permit_code }}</td><td>{{ p.plant_code }}</td><td>{{ p.quota_m3d }}</td>
            <td>{{ p.cod_cap }}</td><td>{{ p.period_label }}</td><td>{{ p.valid_until || '—' }}</td>
            <td>
              <span :class="permitStatusClass(p)">{{ p.status }}</span>
              <span v-if="p.near_expiry_warn" class="warn"> · 临期黄灯</span>
              <span v-if="p.expired" class="fail"> · 已过期</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="muted" style="font-size:0.8rem;margin-top:0.5rem">
        临期/过期许可的准排按钮在准排页与采样页同步置灰，五页同源。
      </p>
    </div>
    <div class="panel">
      <h3>登记许可</h3>
      <div class="form-row"><div><label>许可号</label><input v-model="permitForm.permit_code"/></div>
      <div><label>厂 ID</label><input type="number" v-model.number="permitForm.plant_id"/></div></div>
      <div class="form-row"><div><label>配额</label><input type="number" v-model.number="permitForm.quota_m3d"/></div>
      <div><label>期别</label><input v-model="permitForm.period_label"/></div></div>
      <div class="form-row">
        <div><label>COD帽</label><input type="number" v-model.number="permitForm.cod_cap"/></div>
        <div><label>氨氮帽</label><input type="number" v-model.number="permitForm.nh3n_cap"/></div>
      </div>
      <div class="form-row">
        <div><label>总磷帽</label><input type="number" v-model.number="permitForm.tp_cap"/></div>
        <div><label>有效期至(YYYY-MM-DD)</label><input v-model="permitForm.valid_until" placeholder="2027-12-31"/></div>
      </div>
      <label>临期警告窗(天)</label>
      <input type="number" v-model.number="permitForm.near_expiry_window_days"/>
      <div class="actions"><button @click="createPermit">登记</button></div>
    </div>
  </div>`;
