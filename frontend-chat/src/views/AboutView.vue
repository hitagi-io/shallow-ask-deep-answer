<script setup lang="ts">
import SettingsPage from '../components/layout/SettingsPage.vue'
import BrandMark from '../components/common/BrandMark.vue'
import AppIcon from '../components/common/AppIcon.vue'
const emit = defineEmits<{ back: []; knowledge: [] }>()
</script>

<template>
  <SettingsPage title="关于" description="认识浅问深答，从一个好问题开始。" @back="emit('back')">
    <section class="about-intro" aria-labelledby="about-title">
      <div class="about-brand"><span class="brand-tile"><BrandMark /></span><span>浅问深答</span><span class="product-label">私有知识工作台</span></div>
      <h2 id="about-title">把资料变成知识，<br />让答案有据可循。</h2>
      <p>浅问深答是一个支持多用户的知识库问答项目。你可以按项目整理文档，围绕自己的资料提问，也可以使用通用模式探索更广泛的话题。项目、文档和对话按账号独立管理。</p>
      <button class="btn btn-primary" @click="emit('knowledge')">整理我的知识库 <AppIcon name="arrow-up-right" :size="16" /></button>
    </section>
    <div class="mode-cards">
      <section class="about-card"><span class="section-label">01 / 知识库问答</span><h2>从你的资料中找答案</h2><p>开启输入框下方的“知识库”，选择一个项目后提问。系统检索该项目已入库的文档，并在回答中说明来源。资料不足时，可以补充文档或把问题问得更具体。</p></section>
      <section class="about-card"><span class="section-label">02 / 通用问答</span><h2>探索更广泛的问题</h2><p>关闭“知识库”即可使用通用模式，无需选择项目。回答结合模型知识；服务配置支持时，也可以联网搜索。回答中的蓝色下划线链接可打开来源，帮助你核对原文。</p></section>
    </div>
    <section class="about-card" aria-labelledby="quickstart-title">
      <span class="section-label">开始使用</span><h2 id="quickstart-title">从上传到提问，只需三步</h2>
      <ol class="quickstart"><li><div><h3>创建项目，整理文档</h3><p>从侧边栏进入“知识库”，创建一个项目，上传 TXT、Markdown、PDF、DOCX、XLSX、PPTX 或 PNG、JPG/JPEG、WebP、BMP、TIF/TIFF 图片。TXT 和 Markdown 需使用 UTF-8 编码；图片与扫描 PDF 会自动识别文字，表格资料保留行列关系。</p></div></li><li><div><h3>确认资料已入库</h3><p>在“最近上传”查看处理进度，等待“项目资料”中的文件显示“已入库”。上传或入库期间，该项目暂不能提问。文档更新后可覆盖上传；未入库或索引异常时，展开“索引维护”，使用“检查索引”或“修复与补建”。</p></div></li><li><div><h3>选择项目，开始对话</h3><p>开启新对话，选择知识库模式和对应项目，输入问题。说明背景、范围和期望的回答形式，有助于得到更有用的答案；回答后可查看引用来源和执行过程。</p></div></li></ol>
    </section>
    <section class="about-card" aria-labelledby="help-title">
      <span class="section-label">操作说明</span><h2 id="help-title">常见问题</h2>
      <details open><summary>如何切换模式或知识库项目？</summary><p>在输入框下方切换“知识库”或选择项目。会话第一次发送消息后，模式与项目会固定；后续切换时会开启或复用空白会话，已有记录会保留。</p></details>
      <details><summary>如何查找、重命名或删除对话？</summary><p>左侧显示最近对话，搜索按钮可搜索已加载的对话，底部可加载更多。点击对话旁的“更多操作”可重命名或删除。正在生成回答的对话需要先停止或等待完成再删除。</p></details>
      <details><summary>离开页面或回答失败后怎么办？</summary><p>切换会话、刷新或短暂断网不会主动取消生成；重新打开会话可恢复查看任务状态和已保存的内容。点击输入区的“停止回答”可请求停止。回答失败或已停止时，可点击回答下方的“重新提问”，重新执行该问题。服务进程重启后，原任务不会自动接着生成。</p></details>
      <details><summary>如何复制回答、查看引用？</summary><p>回答下方的复制按钮可复制整段内容，代码块有独立的复制按钮。知识库来源链接集中在文末；点击文件名即可查看提取文本、上下文和高亮证据，同一文件的多处依据可在窗口中切换。PDF 按文件页序定位，Excel 标注工作表和单元格，PPTX 标注幻灯片，其他资料标注标题、行或文本块。OCR 结果可能存在错字，重要数字请结合原始图片核对。来源版本变化或文件被删除后，历史引用可能无法核验。通用回答可通过公开来源链接核对原文。</p></details>
      <details><summary>如何查看回答的执行过程？</summary><p>点击回答下方的“执行过程”，可查看独立显示的总耗时，以及各阶段的状态和耗时。阶段可展开查看子步骤，层级编号表示所属阶段，并行步骤会标明对应编号；阶段耗时已包含子步骤，不应重复相加。遇到问题可复制问题编号用于排查。执行记录保留 14 天，部分历史回答没有记录。</p></details>
      <details><summary>在哪里查看操作记录？</summary><p>点击侧边栏底部的“账号设置”齿轮，选择“操作记录”，即可在独立页面查看当前账号的登录、账号与项目变更、文档操作、回答生成和索引处理结果。记录保留 90 天。</p></details>
      <details><summary>如何修改名字、密码或注销账号？</summary><p>在侧边栏底部的“账号设置”中选择“编辑信息”。名字也是登录用户名；改密需验证当前密码，修改后所有登录会话都需重新登录。注销需阅读须知、验证密码并输入确认文字，最终确认会永久删除账号及其项目、文档、索引和对话。操作记录保留编号、操作类型及结果等信息，至 90 天保留期结束后清理。</p></details>
      <details><summary>删除项目会影响哪些数据？</summary><p>删除项目会清理文档、知识索引和模型上下文。历史对话保留供查看，来源标记为“已删除”，无法再打开原文或继续使用该项目提问。有回答正在生成时无法删除。</p></details>
    </section>
    <p class="about-footer">让每一次提问都有所收获。对于关键结论，请结合引用来源与原始资料进行核对。</p>
  </SettingsPage>
</template>

<style scoped>
.about-intro { padding: 12px 0 32px; max-width: 700px; }
.about-brand { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; font-size: var(--font-heading); font-weight: 600; }
.product-label { border-left: 1px solid var(--line-strong); padding-left: 12px; font-size: var(--font-caption); font-weight: 400; color: var(--muted); }
.about-intro h2 { margin: 24px 0 16px; font-size: clamp(26px, 4vw, 36px); line-height: 1.5; letter-spacing: -.8px; font-weight: 600; }
p { color: var(--muted); font-size: var(--font-small); line-height: 1.9; }
.about-intro > p { font-size: var(--font-body); margin: 0 0 24px; }
.mode-cards { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: var(--section-gap); }
.about-card { padding: var(--panel-padding); margin-bottom: var(--section-gap); border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--bg); }
.section-label { font-size: var(--font-caption); color: var(--muted); letter-spacing: .6px; }
.about-card h2 { margin: 9px 0 14px; font-size: var(--font-heading); font-weight: 600; }
.about-card p { margin: 0; }
.quickstart { list-style: none; counter-reset: step; padding: 0; margin: 24px 0 0; display: grid; gap: 24px; }
.quickstart li { display: flex; gap: 16px; counter-increment: step; }
.quickstart li::before { content: counter(step); display: grid; place-items: center; align-self: flex-start; width: 32px; height: 32px; flex-shrink: 0; background: var(--surface); border-radius: var(--radius-control); font-size: var(--font-small); font-weight: 600; }
h3 { font-size: var(--font-body); font-weight: 600; margin: 2px 0 6px; }
details { border-top: 1px solid var(--line); padding: 16px 0; }
details:last-child { padding-bottom: 0; }
summary { cursor: pointer; font-size: var(--font-body); font-weight: 500; line-height: 1.8; }
details p { margin-top: 12px !important; }
.about-footer { text-align: center; margin: 28px 0 0; font-size: var(--font-caption); }
@media (max-width: 760px) { .mode-cards { grid-template-columns: 1fr; gap: 0; } }
</style>
