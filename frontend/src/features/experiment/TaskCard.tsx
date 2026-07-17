const topicLabels: Record<string, { title: string; description: string }> = {
  weather: { title: "天气", description: "天气查询" },
  physics: { title: "物理常识", description: "你想了解万有引力定律是什么" },
  travelPlan: {
    title: "旅游规划",
    description: "假设你到了某个城市旅游，希望AI规划今日行程/饮食等",
  },
  hiking: {
    title: "短期踏青",
    description: "你计划前往附近短途踏青，希望AI帮你规划路线等",
  },
  news: {
    title: "新闻热点",
    description: "你希望通过AI聊聊了解今天的新闻热点话题",
  },
  tech: {
    title: "科技资讯",
    description: "你希望通过AI聊聊了解最近的科技资讯",
  },
  valueDecision: {
    title: "价值导向决策",
    description:
      "假定你需要AI帮助完成偏价值导向的实际决策\n<em>如：“我明天要参加一个重要面试，天气偏冷、路上要通勤一小时，还要显得稳重专业。你帮我在几套衣服里做个取舍。”</em>",
  },
  taskExecution: {
    title: "明确任务执行",
    description:
      "假定你需要AI帮你执行目标明确的信息整理任务，请你提供需要整理的具体材料\n<em>例如：“请把这段杂乱安排整理成‘日期 / 时间 / 地点 / 任务 / 备注’的日程表：明天上午9点到办公室交材料；午饭前11:20去三楼会议室确认投影；下午15:30记得给窗边绿植浇水；16:10喝水休息一下；17:00参加线上项目会，会议链接在群公告里；18:20到前台取快递，备注是易碎品。”</em>",
  },
  advice: {
    title: "咨询建议",
    description:
      "假定你在日常生活中遇到一些困惑，向AI咨询个人经历或情感相关的问题\n<em>如：“你觉得异地恋能长久吗？”或“如果你是我，你会选择考研还是工作？”</em>",
  },
  goalPlan: {
    title: "目标规划",
    description:
      '假定你有一定学习生活追求目标，如考研/健身等，需要AI辅助制定计划\n<em>如："今年我准备考研，希望你可以给我制定一个复习计划并督促我"</em>',
  },
  funStory: {
    title: "趣事分享",
    description: '就最近在生活中发生的趣事，感受和AI畅谈吧\n<em>如："我最近中了彩票，心情特别好，想和你聊聊"</em>',
  },
  preferenceDecision: {
    title: "主观偏好决策",
    description:
      "假定你想让AI帮你做带有主观偏好的生活决策\n<em>如：“我周末只有半天休息，是去看电影、约朋友喝咖啡，还是一个人散步放空？希望你根据我的心情和风格帮我一起选。”</em>",
  },
  collaborativeExecution: {
    title: "协作任务执行",
    description:
      "假定你希望AI协助完成一项生活表达任务，请你提供要写或要修改的具体内容\n<em>例如：“我想发一条朋友圈，内容是今天工作结束后去散步，感觉轻松了很多。我们一起挑一个自然、有趣、不太矫情的文案吧。”</em>\n<em>例如：“我因为临时有事爽约了朋友，想发一条道歉消息，希望真诚但不要显得太沉重。我们一起改一下措辞吧。”</em>",
  },
};

function topicMarkup(description: string): string {
  return description.replace(/\n/g, "<br>");
}

export function TaskCard({
  topicKey,
  title,
  description,
}: {
  topicKey?: string;
  title?: string;
  description?: string;
}) {
  const topic =
    title && description
      ? { title, description }
      : topicLabels[topicKey ?? ""] ?? {
      title: "实验任务",
      description: "请按照页面提示完成本次实验。",
    };

  return (
    <section className="task-card" aria-label="任务提示">
      <div className="task-card-title">📋 对话主题</div>
      <div
        className="task-card-topic"
        dangerouslySetInnerHTML={{ __html: topicMarkup(topic.description) }}
      />
      <div className="task-card-instruction">
        请以这个主题为核心和 AI 展开相关话题讨论，<strong>请勿跑题</strong>。
      </div>
    </section>
  );
}
