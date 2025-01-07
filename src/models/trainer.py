# src/models/trainer.py
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple
import json
import joblib
import shap
from sklearn.model_selection import train_test_split, cross_val_score, TimeSeriesSplit, GridSearchCV
from sklearn.metrics import accuracy_score, roc_auc_score, classification_report
from sklearn.ensemble import RandomForestClassifier, VotingClassifier
import xgboost as xgb
from lightgbm import LGBMClassifier, log_evaluation
import matplotlib.pyplot as plt
import seaborn as sns
import warnings
import itertools
from copy import deepcopy

class BettingModelTrainer:
    def __init__(self):
        self.model = None
        self.feature_names = None
        self.model_dir = Path(__file__).parent / "saved_models"
        self.model_dir.mkdir(exist_ok=True)
        self.dates = None  # 날짜 정보 저장을 위한 변수 추가
        
        # LightGBM 로그 레벨 조정
        log_evaluation(-1)  # 모든 LightGBM 로그 비활성화
    
    def prepare_features(self, data: List[Dict]) -> Tuple[pd.DataFrame, pd.Series]:
        """데이터에서 특성과 레이블 추출"""
        df = pd.DataFrame(data)
        
        # 날짜 기준으로 정렬
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date')
        self.dates = df['date']  # 날짜 정보 저장
        
        # 승패 레이블 생성 (홈팀 기준)
        y = (df['home_team_score'] > df['away_team_score']).astype(int)
        
        # 특 ID를 범주형으로 변환
        home_team_dummies = pd.get_dummies(df['home_team_id'], prefix='home_team')
        away_team_dummies = pd.get_dummies(df['away_team_id'], prefix='away_team')
        
        # 특성 선택
        features = [
            # 기본 경기력 지표
            'home_rebounds', 'away_rebounds',
            'home_assists', 'away_assists',
            'home_fieldGoalsAttempted', 'away_fieldGoalsAttempted',
            'home_fieldGoalsMade', 'away_fieldGoalsMade',
            'home_fieldGoalPct', 'away_fieldGoalPct',
            'home_freeThrowsAttempted', 'away_freeThrowsAttempted',
            'home_freeThrowsMade', 'away_freeThrowsMade',
            'home_freeThrowPct', 'away_freeThrowPct',
            'home_threePointFieldGoalsAttempted', 'away_threePointFieldGoalsAttempted',
            'home_threePointFieldGoalsMade', 'away_threePointFieldGoalsMade',
            'home_threePointPct', 'away_threePointPct',
            
            # 리더 통계
            'home_leader_points', 'away_leader_points',
            'home_leader_rebounds', 'away_leader_rebounds',
            'home_leader_assists', 'away_leader_assists',
            
            # 팀 기록
            'home_overall_record_win_rate', 'away_overall_record_win_rate',
            'home_home_record_win_rate', 'away_home_record_win_rate',
            'home_road_record_win_rate', 'away_road_record_win_rate',
            'home_vs_away_win_rate',
            
            # 최근 트렌드
            'home_recent_win_rate', 'away_recent_win_rate',
            'home_recent_avg_score', 'away_recent_avg_score',
            'home_recent_home_win_rate', 'away_recent_home_win_rate',
            'home_recent_away_win_rate', 'away_recent_away_win_rate',
            
            # 컨디션
            'home_rest_days', 'away_rest_days'
        ]
        
        X = df[features]
        
        # One-Hot Encoded 팀 ID 추가
        X = pd.concat([X, home_team_dummies, away_team_dummies], axis=1)
        
        self.feature_names = X.columns.tolist()
        
        return X, y
    
    def train(self, X: pd.DataFrame, y: pd.Series, initial_train: bool = True) -> Dict:
        """앙상블 모델 학습 및 평가"""
        warnings.filterwarnings('ignore', category=UserWarning)
        warnings.filterwarnings('ignore', message='.*best gain.*')
        
        eval_size = int(len(X) * 0.2)
        X_eval = X[-eval_size:]
        y_eval = y[-eval_size:]
        
        if initial_train:
            # XGBoost 최적화
            xgb_best_params = self.optimize_hyperparameters(X[:-eval_size], y[:-eval_size], 'xgboost')
            print("\n=== XGBoost 최적 파라미터 ===")
            print(xgb_best_params)
            
            # LightGBM 최적화
            lgb_best_params = self.optimize_hyperparameters(X[:-eval_size], y[:-eval_size], 'lightgbm')
            print("\n=== LightGBM 최적 파라미터 ===")
            print(lgb_best_params)
            
            # RandomForest 최적화
            rf_best_params = self.optimize_hyperparameters(X[:-eval_size], y[:-eval_size], 'randomforest')
            print("\n=== RandomForest 최적 파라미터 ===")
            print(rf_best_params)
            
            # 최적화된 시간 가중치 적용 (XGBoost 기준)
            time_weights = np.linspace(
                xgb_best_params['weight_start'], 
                xgb_best_params['weight_end'], 
                len(X[:-eval_size])
            )
            
            # 시각화를 위한 파라미터 준비
            model_params = {
                'XGBoost': xgb_best_params,
                'LightGBM': lgb_best_params,
                'RandomForest': rf_best_params
            }
            
            # 1. 초기 모델 생성 및 학습
            xgb_model = xgb.XGBClassifier(
                learning_rate=xgb_best_params['learning_rate'],
                n_estimators=xgb_best_params['n_estimators'],
                random_state=42
            )
            lgb_model = LGBMClassifier(
                learning_rate=lgb_best_params['learning_rate'],
                n_estimators=lgb_best_params['n_estimators'],
                random_state=42,
                verbose=-1
            )
            rf_model = RandomForestClassifier(
                n_estimators=rf_best_params['n_estimators'],
                max_depth=rf_best_params['max_depth'],
                random_state=42
            )
            
            # 각 모델 학습
            xgb_model.fit(X[:-eval_size], y[:-eval_size], sample_weight=time_weights)
            lgb_model.fit(X[:-eval_size], y[:-eval_size], sample_weight=time_weights)
            rf_model.fit(X[:-eval_size], y[:-eval_size], sample_weight=time_weights)
            
            # 2. 초기 앙상블 모델 생성 및 학습 (정확도 0.842)
            self.model = VotingClassifier(
                estimators=[
                    ('xgb', xgb_model),
                    ('lgb', lgb_model),
                    ('rf', rf_model)
                ],
                voting='soft'
            )
            self.model.fit(X[:-eval_size], y[:-eval_size], sample_weight=time_weights)
            
            # 3. 초기 앙상블 모델의 특성 중요도 시각화
            initial_models = {
                'XGBoost': xgb_model,
                'LightGBM': lgb_model,
                'RandomForest': rf_model
            }
            importance_dict = self.visualize_feature_importance(
                initial_models,
                self.model_dir / 'initial_ensemble_feature_importance.png'
            )
            
            # 초기 모델의 특성 중요도를 JSON으로 저장
            with open(self.model_dir / 'initial_feature_importance.json', 'w') as f:
                json.dump(importance_dict, f, indent=4)
            
            # model_params 저장
            self.model_params = model_params
            
            # 초기 모델의 시간 가중치 시각화 추가
            self.visualize_time_weights(
                model_params,  # 초기 모델의 파라미터
                self.dates[:-eval_size], 
                self.model_dir / 'initial_time_weights_comparison.png'
            )
            
        else:
            # 4. 파인튜닝된 모델 시각화
            finetuned_models = {
                'XGBoost': self.model.estimators_[0],
                'LightGBM': self.model.estimators_[1],
                'RandomForest': self.model.estimators_[2]
            }
            
            self.visualize_time_weights(
                self.model_params,  # fine_tune()에서 저장된 파인튜닝된 파라미터
                self.dates[:-eval_size], 
                self.model_dir / 'finetuned_time_weights_comparison.png'
            )
            
            importance_dict = self.visualize_feature_importance(
                finetuned_models,  # 정확도 0.956인 파인튜닝된 앙상블 모델
                self.model_dir / 'finetuned_ensemble_feature_importance.png'
            )
            
            # 파인튜닝된 모델의 특성 중요도를 JSON으로 저장
            with open(self.model_dir / 'finetuned_feature_importance.json', 'w') as f:
                json.dump(importance_dict, f, indent=4)
        
        # 최근 20% 데이터로 성능 평가
        y_pred = self.model.predict(X_eval)
        y_pred_proba = self.model.predict_proba(X_eval)
        
        # 날짜별 예측 정확도 분석
        date_accuracy = pd.DataFrame({
            'date': self.dates[-eval_size:],
            'actual': y_eval,
            'predicted': y_pred
        })
        date_accuracy['correct'] = (date_accuracy['actual'] == date_accuracy['predicted']).astype(int)
        
        # 앙상블 모델의 정확도 추이
        plt.figure(figsize=(12, 6))
        date_accuracy.set_index('date')['correct'].rolling('7D', min_periods=1).mean().plot()
        plt.title('7-Day Rolling Average Prediction Accuracy')
        plt.xlabel('Date')
        plt.ylabel('Accuracy')
        plt.grid(True)
        plt.tight_layout()
        accuracy_plot_name = 'initial_accuracy_trend.png' if initial_train else 'finetuned_accuracy_trend.png'
        plt.savefig(self.model_dir / accuracy_plot_name)
        plt.close()
        
        metrics = {
            'accuracy': accuracy_score(y_eval, y_pred),
            'roc_auc': roc_auc_score(y_eval, y_pred_proba[:, 1]),
            'classification_report': classification_report(y_eval, y_pred),
            'eval_size': eval_size,
            'total_size': len(X),
            'time_analysis': {
                'early_accuracy': date_accuracy['correct'][:eval_size//2].mean(),
                'late_accuracy': date_accuracy['correct'][eval_size//2:].mean()
            }
        }
        
        # 교차 검증은 전체 데이터에 대해 수행
        cv_scores = cross_val_score(self.model, X, y, cv=5)
        metrics['cv_scores'] = cv_scores
        metrics['cv_mean'] = cv_scores.mean()
        metrics['cv_std'] = cv_scores.std()
        
        # 최근 20% 데이터로 성능 평가 후
        if initial_train:
            # 초기 모델의 특성 중요도 분석
            importance_dict = self.analyze_feature_importance(X, 'initial_feature_importance.png')
        else:
            # 파인튜닝된 모델의 특성 중요도 분석
            importance_dict = {}
            for i, feat in enumerate(self.feature_names):
                total_importance = 0
                for model in self.model.estimators_:  # named_estimators_ 대신 estimators_ 사용
                    if hasattr(model, 'feature_importances_'):
                        importances = model.feature_importances_
                        importances = importances / np.sum(importances)
                        total_importance += importances[i]
                importance_dict[feat] = total_importance / 3
        
        # 상위 5개 특성 출력
        print("\n=== 상위 5개 중요 특성 ===")
        for feature, importance in sorted(
            importance_dict.items(), 
            key=lambda x: x[1], 
            reverse=True
        )[:5]:
            print(f"{feature}: {importance:.3f}")
        
        return metrics
    
    def analyze_feature_importance(self, X: pd.DataFrame, plot_name: str) -> Dict:
        """특성 중요도 분석"""
        if self.model is None:
            raise ValueError("모델이 학습되지 않았습니다.")
        
        # 각 모델의 특성 중요도를 정규화하여 계산
        xgb_model = self.model.named_estimators_['xgb']
        lgb_model = self.model.named_estimators_['lgb']
        rf_model = self.model.named_estimators_['rf']
        
        xgb_importance = xgb_model.feature_importances_
        lgb_importance = lgb_model.feature_importances_
        rf_importance = rf_model.feature_importances_
        
        # 각각의 특성 중요도를 0-1 사이로 정규화
        xgb_importance = xgb_importance / np.sum(xgb_importance)
        lgb_importance = lgb_importance / np.sum(lgb_importance)
        rf_importance = rf_importance / np.sum(rf_importance)
        
        # 정규화된 값들의 평균 계산
        importance_values = (xgb_importance + lgb_importance + rf_importance) / 3
        importance_dict = dict(zip(self.feature_names, importance_values))
        
        # # 특성 중요도 시각화 및 저장
        # plt.figure(figsize=(12, 6))
        # sorted_idx = np.argsort(importance_values)
        # pos = np.arange(sorted_idx.shape[0]) + .5
        # plt.barh(pos, importance_values[sorted_idx])
        # plt.yticks(pos, np.array(self.feature_names)[sorted_idx])
        # plt.xlabel('Feature Importance (Average)')
        # plt.title('Ensemble Model Feature Importance')
        # plt.tight_layout()
        # plt.savefig(self.model_dir / plot_name)
        # plt.close()
        
        return importance_dict
    
    def save_model(self, timestamp: str):
        """모델 저장"""
        if self.model is None:
            raise ValueError("모델이 학습되지 않았습니다.")
        
        model_path = self.model_dir / f"betting_model_{timestamp}.joblib"
        joblib.dump(self.model, model_path)
        print(f"모델이 저장되었습니다: {model_path}")
    
    def fine_tune(self, X: pd.DataFrame, y: pd.Series, n_recent_games: int = 100) -> None:
        """최근 N경기 데이터로 모델 파인튜닝"""
        if self.model is None:
            raise ValueError("기존 학습된 모델이 없습니다.")
        
        # 최근 N경기 선택
        X_recent = X[-n_recent_games:]
        y_recent = y[-n_recent_games:]
        
        # 각 모델별 파인튜닝 수행
        xgb_best_model, xgb_best_params = self._fine_tune_model(
            X_recent, y_recent, 'xgboost'
        )
        print("\n=== XGBoost 파인튜닝 결과 ===")
        print(f"파라미터: {xgb_best_params}")
        print(f"CV 평균 정확도: {xgb_best_params['cv_accuracy']:.3f}")
        print(f"후기 데이터 정확도: {xgb_best_params['late_accuracy']:.3f}")
        
        lgb_best_model, lgb_best_params = self._fine_tune_model(
            X_recent, y_recent, 'lightgbm'
        )
        print("\n=== LightGBM 파인튜닝 결과 ===")
        print(f"파라미터: {lgb_best_params}")
        print(f"CV 평균 정확도: {lgb_best_params['cv_accuracy']:.3f}")
        print(f"후기 데이터 정확도: {lgb_best_params['late_accuracy']:.3f}")
        
        rf_best_model, rf_best_params = self._fine_tune_model(
            X_recent, y_recent, 'randomforest'
        )
        print("\n=== RandomForest 파인튜닝 결과 ===")
        print(f"파라미터: {rf_best_params}")
        print(f"CV 평균 정확도: {rf_best_params['cv_accuracy']:.3f}")
        print(f"후기 데이터 정확도: {rf_best_params['late_accuracy']:.3f}")
        
        # VotingClassifier 재구성
        self.model.estimators_ = [xgb_best_model, lgb_best_model, rf_best_model]
        
        # 파인튜닝된 파라미터 저장
        self.model_params = {
            'XGBoost': xgb_best_params,
            'LightGBM': lgb_best_params,
            'RandomForest': rf_best_params
        }
    
    def _fine_tune_model(self, X: pd.DataFrame, y: pd.Series, model_type: str):
        """개별 모델 파인튜닝"""
        # 하이퍼파라미터 그리드 설정
        weight_param_grid = {
            'weight_start': [0.2, 0.3, 0.4],
            'weight_end': [0.8, 0.9, 1.0],
        }
        
        if model_type in ['xgboost', 'lightgbm']:
            model_param_grid = {
                'learning_rate': [0.01, 0.03],
                'n_estimators': [100, 200]
            }
        else:  # randomforest
            model_param_grid = {
                'n_estimators': [100, 200],
                'max_depth': [10, 20]
            }
        
        tscv = TimeSeriesSplit(n_splits=3)
        best_score = 0
        best_model = None
        best_params = {}
        
        # 파라미터 조합 테스트
        for w_start in weight_param_grid['weight_start']:
            for w_end in weight_param_grid['weight_end']:
                time_weights = np.linspace(w_start, w_end, len(X))
                
                for param_combo in [dict(zip(model_param_grid.keys(), v)) 
                                  for v in itertools.product(*model_param_grid.values())]:
                    
                    # 모델 생성
                    if model_type == 'xgboost':
                        model = xgb.XGBClassifier(**param_combo, random_state=42)
                    elif model_type == 'lightgbm':
                        model = LGBMClassifier(**param_combo, random_state=42, verbose=-1)
                    else:
                        model = RandomForestClassifier(**param_combo, random_state=42)
                    
                    # CV 평균 정확도 계산
                    cv_scores = cross_val_score(model, X, y, cv=tscv)
                    cv_accuracy = cv_scores.mean()
                    
                    # 후기 데이터 정확도 계산
                    eval_size = int(len(X) * 0.2)
                    model.fit(X[:-eval_size], y[:-eval_size], 
                             sample_weight=time_weights[:-eval_size])
                    y_pred = model.predict(X[-eval_size:])
                    late_accuracy = accuracy_score(y[-eval_size:], y_pred)
                    
                    # 최종 점수 계산
                    final_score = (cv_accuracy + late_accuracy) / 2
                    
                    if final_score > best_score:
                        best_score = final_score
                        best_model = model
                        best_params = {
                            'weight_start': w_start,
                            'weight_end': w_end,
                            'cv_accuracy': cv_accuracy,
                            'late_accuracy': late_accuracy,
                            **param_combo
                        }
        
        return best_model, best_params
    
    def optimize_hyperparameters(self, X: pd.DataFrame, y: pd.Series, model_type: str = 'xgboost') -> Dict:
        """시간 가중치 중심의 하이퍼파라미터 최적화"""
        
        # 시간 가중치 관련 파라미터
        weight_param_grid = {
            'weight_start': [0.2, 0.3, 0.4],
            'weight_end': [0.8, 0.9, 1.0],
        }
        
        # 모델별 파라미터 그리드
        if model_type == 'xgboost':
            model_param_grid = {
                'learning_rate': [0.01, 0.03],
                'n_estimators': [100, 200]
            }
        elif model_type == 'lightgbm':
            model_param_grid = {
                'learning_rate': [0.01, 0.03],
                'n_estimators': [100, 200]
            }
        else:  # randomforest
            model_param_grid = {
                'n_estimators': [100, 200],
                'max_depth': [10, 20]
            }
        
        tscv = TimeSeriesSplit(n_splits=3)
        best_score = 0
        best_params = {}
        
        # 시간 가중치 조합 테스트
        for w_start in weight_param_grid['weight_start']:
            for w_end in weight_param_grid['weight_end']:
                time_weights = np.linspace(w_start, w_end, len(X))
                
                for param_combo in [dict(zip(model_param_grid.keys(), v)) 
                                  for v in itertools.product(*model_param_grid.values())]:
                    
                    # 모델 생성
                    if model_type == 'xgboost':
                        model = xgb.XGBClassifier(**param_combo, random_state=42)
                    elif model_type == 'lightgbm':
                        model = LGBMClassifier(**param_combo, random_state=42, verbose=-1)
                    else:
                        model = RandomForestClassifier(**param_combo, random_state=42)
                    
                    # 교차 검증 수행
                    scores = []
                    for train_idx, val_idx in tscv.split(X):
                        X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
                        y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]
                        w_train = time_weights[train_idx]
                        
                        model.fit(X_train, y_train, sample_weight=w_train)
                        
                        y_pred = model.predict(X_val)
                        early_acc = accuracy_score(y_val[:len(y_val)//2], 
                                                y_pred[:len(y_pred)//2])
                        late_acc = accuracy_score(y_val[len(y_val)//2:], 
                                               y_pred[len(y_pred)//2:])
                        score = (early_acc + late_acc) / 2
                        scores.append(score)
                    
                    avg_score = np.mean(scores)
                    if avg_score > best_score:
                        best_score = avg_score
                        best_params = {
                            'weight_start': w_start,
                            'weight_end': w_end,
                            **param_combo
                        }
        
        return best_params
    
    def visualize_time_weights(self, model_params, dates, save_path):
        """각 모델의 시간 가중치 비교 시각화"""
        plt.figure(figsize=(12, 6))
        
        for model_name, params in model_params.items():
            # 파인튜닝된 모델의 경우 기본값 사용
            if 'weight_start' not in params:
                weight_start = 0.2  # 기본값
                weight_end = 0.8    # 기본값
            else:
                weight_start = params['weight_start']
                weight_end = params['weight_end']
            
            weights = np.linspace(weight_start, weight_end, len(dates))
            plt.plot(dates, weights, label=model_name)
        
        plt.title('Time Weights Comparison')
        plt.xlabel('Date')
        plt.ylabel('Weight')
        plt.legend()
        plt.xticks(rotation=45)
        plt.tight_layout()
        plt.savefig(save_path)
        plt.close()
    
    def visualize_feature_importance(self, models, save_path):
        """앙상블 모델의 특성 중요도 시각화"""
        plt.figure(figsize=(15, 12))
        
        importance_dict = {}
        for name, model in models.items():
            if hasattr(model, 'feature_importances_'):
                importances = model.feature_importances_
                importances = importances / np.sum(importances)  # 정규화
                
                for feat, imp in zip(self.feature_names, importances):
                    importance_dict[feat] = importance_dict.get(feat, 0) + imp/3
        
        # 상위 20개 특성 선택
        top_features = dict(sorted(importance_dict.items(), 
                                 key=lambda x: x[1], 
                                 reverse=True)[:20])
        
        # 바 차트 생성
        bars = plt.barh(list(top_features.keys()), list(top_features.values()))
        
        # 각 바 옆에 수치 표시
        for i, bar in enumerate(bars):
            width = bar.get_width()
            plt.text(width, bar.get_y() + bar.get_height()/2, 
                    f'{width:.4f}', 
                    ha='left', va='center', fontsize=10)
        
        plt.title('Ensemble Feature Importance (Top 20)')
        plt.xlabel('Importance')
        plt.tight_layout()
        plt.savefig(save_path)
        plt.close()
        
        return importance_dict
    
    def visualize_model_predictions(self, X_eval, y_eval, models, dates, save_path):
        """각 모델의 예측 성능 비교 시각화"""
        plt.figure(figsize=(15, 8))
        
        for name, model in models.items():
            y_pred = model.predict(X_eval)
            correct = (y_pred == y_eval).astype(int)
            plt.plot(dates, pd.Series(correct).rolling(7).mean(), 
                    label=f'{name} Accuracy', alpha=0.7)
        
        plt.title('Model Predictions Comparison')
        plt.xlabel('Date')
        plt.ylabel('7-Day Rolling Accuracy')
        plt.legend()
        plt.grid(True)
        plt.tight_layout()
        plt.savefig(save_path)
        plt.close()

def get_latest_processed_data() -> List[Dict]:
    """src/data 폴더에서 가장 최신의 processed json 파일 로드"""
    data_dir = Path(__file__).parent.parent / "data"
    json_files = list(data_dir.glob("processed_*.json"))
    
    if not json_files:
        raise FileNotFoundError("처리된 데이터 파일을 찾을 수 없습니다.")
    
    latest_file = max(json_files, key=lambda x: x.stat().st_mtime)
    print(f"데이터 파일 로드: {latest_file.name}")
    
    with open(latest_file, 'r') as f:
        return json.load(f)

# 테스트 코드
if __name__ == "__main__":
    # 최신 데이터 로드
    data = get_latest_processed_data()
    
    # 모델 학습
    trainer = BettingModelTrainer()
    X, y = trainer.prepare_features(data)
    
    # 초기 모델 학습
    print("\n=== 초기 모델 학습 ===")
    metrics = trainer.train(X, y, initial_train=True)
    print("\n=== 초기 모델 성능 ===")
    print(f"정확도: {metrics['accuracy']:.3f}")
    print(f"ROC-AUC: {metrics['roc_auc']:.3f}")
    
    # 최근 100경기로 파인튜닝
    print("\n=== 파인튜닝 시작 ===")
    trainer.fine_tune(X, y, n_recent_games=100)
    
    # 파인튜닝된 모델 평가
    print("\n=== 파인튜닝된 모델 평가 ===")
    metrics = trainer.train(X, y, initial_train=False)  # 기존 모델 유지
    
    # 결과 출력 (파인튜닝된 모델의 성능)
    print("\n=== 모델 성능 ===")
    print(f"정확도: {metrics['accuracy']:.3f}")
    print(f"ROC-AUC: {metrics['roc_auc']:.3f}")
    print(f"\n=== 평가 데이터 크기 ===")
    print(f"전체 데이터: {metrics['total_size']}")
    print(f"평가 데이터: {metrics['eval_size']}")
    print("\n=== 교차 검증 결과 ===")
    print(f"CV 평균 정확도: {metrics['cv_mean']:.3f} (+/- {metrics['cv_std']*2:.3f})")
    print("\n=== 분류 리포트 ===")
    print(metrics['classification_report'])
    print("\n=== 시간 기반 분석 ===")
    print(f"초기 평가 데이터 정확도: {metrics['time_analysis']['early_accuracy']:.3f}")
    print(f"후기 평가 데이터 정확도: {metrics['time_analysis']['late_accuracy']:.3f}")
    
    
    # 파인튜닝된 모델 저장
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trainer.save_model(timestamp)