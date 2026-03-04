export interface MiniAppCompanySummary {
  id: number;
  name: string;
  company_type: string;
  level: number;
  employee_count: number;
  daily_revenue: number;
  total_funds: number;
}

export interface MiniAppProductSummary {
  id: number;
  name: string;
  quality: number;
  daily_income: number;
  version: number;
}

export interface MiniAppActiveCompany extends MiniAppCompanySummary {
  shareholder_count: number;
  product_count: number;
  completed_research_count: number;
  top_products: MiniAppProductSummary[];
}

export interface MiniAppPreload {
  user: {
    id: number;
    tg_id: number;
    name: string;
    traffic: number;
    reputation: number;
    points: number;
  } | null;
  companies: MiniAppCompanySummary[];
  active_company: MiniAppActiveCompany | null;
  meta: {
    preloaded_at: string;
    company_count?: number;
    missing_user?: boolean;
  };
}

export interface MiniAppAuthResponse {
  session_token: string;
  expires_in: number;
  preload: MiniAppPreload;
}
