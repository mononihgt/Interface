export type PretestItem =
  | { id: string; no?: number; type: "likert"; text: string }
  | {
      id: string;
      no?: number;
      type: "slider";
      min: number;
      max: number;
      text: string;
      note?: string;
    }
  | { id: string; no?: number; type: "frequency"; text: string };

export interface PretestSection {
  instruction?: string;
  items: PretestItem[];
}

export const likertValues = [1, 2, 3, 4, 5] as const;

export const frequencyOptions = [
  { value: "A", label: "每天不止一次" },
  { value: "B", label: "大约每天一次" },
  { value: "C", label: "大约每周一次" },
  { value: "D", label: "大约每两周一次" },
  { value: "E", label: "大约每月一次" },
  { value: "F", label: "每月不到一次" },
  { value: "G", label: "我只尝试过几次" },
  { value: "H", label: "我从未使用过" },
] as const;

const agencyItems: PretestItem[] = [
  { id: "q27", no: 27, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上完成计划？" },
  { id: "q28", no: 28, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上做出选择？" },
  { id: "q29", no: 29, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上进行考量？" },
  { id: "q30", no: 30, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上做出决定？" },
  { id: "q31", no: 31, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上进行沟通？" },
  { id: "q32", no: 32, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上拥有认知？" },
  { id: "q33", no: 33, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上拥有记忆？" },
  { id: "q34", no: 34, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上完成推理？" },
  { id: "q35", no: 35, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上拥有智能？" },
  { id: "q36", no: 36, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上进行注意？" },
  { id: "q37", no: 37, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上感到释然？" },
  { id: "q38", no: 38, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上感到愉悦？" },
  { id: "q39", no: 39, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上感到愧疚？" },
  { id: "q40", no: 40, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上感到激情？" },
  { id: "q41", no: 41, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上感到幸福？" },
  { id: "q42", no: 42, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上感到后悔？" },
  { id: "q43", no: 43, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上感到怨恨？" },
  { id: "q44", no: 44, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上感到享受？" },
  { id: "q45", no: 45, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上感到恐惧？" },
  { id: "q46", no: 46, type: "slider", min: 1, max: 100, text: "你觉得AI助手能多大程度上感到钦佩？" },
];

export const pretestSections: PretestSection[] = [
  {
    instruction:
      "请根据你的实际情况，评估以下每一条陈述与你的相符程度，评分标准为 1-5 分，其中1表示非常不符合，5 表示非常符合。",
    items: [
      { id: "q1", no: 1, type: "likert", text: "我总是派对上的焦点人物。" },
      { id: "q2", no: 2, type: "likert", text: "我会同情别人的感受。" },
      { id: "q3", no: 3, type: "likert", text: "我总会马上完成该做的事。" },
      { id: "q4", no: 4, type: "likert", text: "我的情绪波动频繁。" },
      { id: "q5", no: 5, type: "likert", text: "我拥有丰富的想象力。" },
      { id: "q6", no: 6, type: "likert", text: "我话不多。" },
      { id: "q7", no: 7, type: "likert", text: "我对别人的问题不太关心。" },
      { id: "q8", no: 8, type: "likert", text: "我经常忘记把东西放回原处。" },
      { id: "q9", no: 9, type: "likert", text: "我大多数时候都很放松。" },
      { id: "q10", no: 10, type: "likert", text: "我对抽象的概念不太感兴趣。" },
      { id: "q11", no: 11, type: "likert", text: "我在派对和聚会上喜欢与很多不同的人交谈。" },
      { id: "q12", no: 12, type: "likert", text: "我能感受到别人的情绪。" },
      { id: "q13", no: 13, type: "likert", text: "我喜欢有条理的生活。" },
      { id: "q14", no: 14, type: "likert", text: "我很容易心烦意乱。" },
      { id: "q15", no: 15, type: "likert", text: "我难以理解抽象概念。" },
      { id: "q16", no: 16, type: "likert", text: "我喜欢待在不引人注意的地方。" },
      { id: "q17", no: 17, type: "likert", text: "我对别人并不是真的感兴趣。" },
      { id: "q18", no: 18, type: "likert", text: "我经常把事情搞得一团糟。" },
      { id: "q19", no: 19, type: "likert", text: "我很少感到沮丧。" },
      { id: "q20", no: 20, type: "likert", text: "我的想象力一般。" },
    ],
  },
  {
    instruction:
      "请根据你的实际情况，评估您对以下表述的同意程度，评分标准为1-5分，1分表示非常不同意，5分表示非常同意",
    items: [
      { id: "q21", no: 21, type: "likert", text: "总的来说，我信任AI助手" },
      { id: "q22", no: 22, type: "likert", text: "AI助手帮助我解决了许多问题" },
      { id: "q23", no: 23, type: "likert", text: "我认为依赖AI助手寻求帮助是个好主意" },
      { id: "q24", no: 24, type: "likert", text: "我不相信从AI助手那里获取的信息" },
      { id: "q25", no: 25, type: "likert", text: "AI助手是可靠的" },
      { id: "q26", no: 26, type: "likert", text: "我依赖AI助手" },
    ],
  },
  {
    instruction:
      "请根据你的真实感受，对以下每一个问题进行评分，评分标准为1-100分，其中1表示完全不能，50表示一定程度上能，100表示完全能。",
    items: agencyItems.flatMap((item) => {
      const confidenceText =
        item.id === "q27"
          ? "对于这个问题,你对自己的回答有多大的信心?(1分-完全没有信心;100分-非常有信心)"
          : "对于这个问题,你对自己的回答有多大的信心?";

      return [
        item,
        {
          id: `confidence_${item.id}`,
          type: "slider" as const,
          min: 1,
          max: 100,
          text: confidenceText,
        },
      ];
    }),
  },
  {
    items: [
      {
        id: "q47",
        no: 47,
        type: "slider",
        min: 1,
        max: 100,
        text: "请评估AI助手是否具有主观意识体验能力（例如：能否真正感受到疼痛、快乐等情感）：",
        note: "1分 = 完全不具备主观意识体验\n100分 = 完全具备主观意识体验",
      },
    ],
  },
  {
    items: [
      {
        id: "q48",
        no: 48,
        type: "frequency",
        text: "你使用智能语音助手（例如华为的小艺，小米的小爱同学，苹果的Siri，百度的小度，阿里巴巴的天猫精灵，OPPO的小布助手，vivo的Jovi）的频率是？",
      },
    ],
  },
  {
    items: [
      {
        id: "q49",
        no: 49,
        type: "frequency",
        text: "你使用AI聊天机器人（例如文心一言、通义千问、豆包、智谱清言、Kimi、ChatGPT、Claude、Gemini）的频率是？",
      },
    ],
  },
];

export const PRETEST_CONFIG = {
  likertValues,
  frequencyOptions,
  sections: pretestSections,
} as const;

export function getAllPretestItems(): PretestItem[] {
  return pretestSections.flatMap((section) => section.items);
}

export function getRequiredPretestItemIds(): string[] {
  return getAllPretestItems().map((item) => item.id);
}

export function getRequiredSliderItemIds(): string[] {
  return getAllPretestItems()
    .filter((item) => item.type === "slider")
    .map((item) => item.id);
}
