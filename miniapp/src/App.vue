<script setup lang="ts">
import { computed, onMounted } from 'vue';

import { useBootstrapStore } from './stores/bootstrap';

const store = useBootstrapStore();

onMounted(() => {
  void store.bootstrap();
});

const user = computed(() => store.preload?.user);
const activeCompany = computed(() => store.preload?.active_company);
const companies = computed(() => store.preload?.companies || []);

const fmt = (num: number | undefined) => {
  if (typeof num !== 'number') {
    return '-';
  }
  return new Intl.NumberFormat('zh-CN').format(num);
};
</script>

<template>
  <div class="app-shell">
    <van-nav-bar title="��ҵ�۹� MiniApp" fixed placeholder />

    <main class="page">
      <van-pull-refresh
        :model-value="store.refreshing"
        @refresh="() => store.refresh()"
      >
        <section v-if="store.loading" class="skeleton-wrap">
          <van-skeleton title :row="4" />
          <van-skeleton title :row="4" />
        </section>

        <section v-else-if="store.error" class="error-wrap">
          <van-empty description="����ʧ�ܣ�������" />
          <p class="error-text">{{ store.error }}</p>
          <van-button type="primary" block @click="() => store.bootstrap()">
            ���¼���
          </van-button>
        </section>

        <section v-else class="content-wrap">
          <van-card class="card">
            <template #title>�����Ϣ</template>
            <template #desc>
              <div class="line">�ǳƣ�{{ user?.name || '-' }}</div>
              <div class="line">��ң�{{ fmt(user?.self_points) }}</div>
              <div class="line">������{{ fmt(user?.reputation) }}</div>
            </template>
          </van-card>

          <van-card v-if="activeCompany" class="card">
            <template #title>��ǰ��˾</template>
            <template #desc>
              <div class="line title-row">
                {{ activeCompany.name }}
                <van-tag type="primary">Lv.{{ activeCompany.level }}</van-tag>
              </div>
              <div class="line">��ҵ��{{ activeCompany.company_type }}</div>
              <div class="line">Ա����{{ fmt(activeCompany.employee_count) }}</div>
              <div class="line">��Ӫ�գ�{{ fmt(activeCompany.daily_revenue) }}</div>
              <div class="line">�ʽ�{{ fmt(activeCompany.cp_points) }}</div>
              <div class="line">�ɶ���{{ fmt(activeCompany.shareholder_count) }}</div>
              <div class="line">��Ʒ��{{ fmt(activeCompany.product_count) }}</div>
              <div class="line">�Ƽ���{{ fmt(activeCompany.completed_research_count) }}</div>
            </template>
          </van-card>

          <van-card v-if="activeCompany?.top_products?.length" class="card">
            <template #title>Top ��Ʒ</template>
            <template #desc>
              <van-cell-group inset>
                <van-cell
                  v-for="product in activeCompany.top_products"
                  :key="product.id"
                  :title="`${product.name} �� v${product.version}`"
                  :label="`���� ${product.quality}`"
                  :value="`���� ${fmt(product.daily_income)}`"
                />
              </van-cell-group>
            </template>
          </van-card>

          <van-card class="card">
            <template #title>��˾�б�</template>
            <template #desc>
              <van-cell-group inset>
                <van-cell
                  v-for="company in companies"
                  :key="company.id"
                  :title="company.name"
                  :label="`��ҵ ${company.company_type} �� Lv.${company.level}`"
                  :value="`�ʽ� ${fmt(company.cp_points)}`"
                />
              </van-cell-group>
            </template>
          </van-card>
        </section>
      </van-pull-refresh>
    </main>
  </div>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  background: linear-gradient(180deg, #f4f7fb 0%, #ffffff 45%);
}

.page {
  padding: 12px;
}

.skeleton-wrap,
.error-wrap,
.content-wrap {
  display: grid;
  gap: 12px;
}

.error-text {
  margin: 0;
  color: #9f1239;
  font-size: 13px;
  line-height: 1.4;
  text-align: center;
}

.card {
  border-radius: 14px;
  box-shadow: 0 6px 20px rgba(13, 38, 76, 0.08);
}

.line {
  margin-bottom: 6px;
  color: #1f2937;
  font-size: 14px;
  line-height: 1.35;
}

.title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
}
</style>
