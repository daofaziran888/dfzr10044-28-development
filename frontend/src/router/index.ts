import { createRouter, createWebHistory } from 'vue-router'

import ColumnProfileView from '@/views/ColumnProfileView.vue'
import CompactCandidateView from '@/views/CompactCandidateView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 定名子路径排在 /:moduleKey 之前，避免被动态段抢先匹配。
    { path: '/column_stat/profiles', name: 'column-profiles', component: ColumnProfileView },
    { path: '/compact_job/candidates', name: 'compact-candidates', component: CompactCandidateView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
