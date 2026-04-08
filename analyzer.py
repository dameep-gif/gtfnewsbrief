from textblob import TextBlob
import re

def analyze_sentiment(text):
    """
    감정 분석 (긍정/부정/중립)
    
    Args:
        text: 분석할 텍스트
    
    Returns:
        dict: 감정 결과 및 신뢰도
    """
    try:
        # 영문 텍스트 분석
        blob = TextBlob(text)
        polarity = blob.sentiment.polarity
        
        # 한국어 간단 감정 분석 (키워드 기반)
        positive_words = ['좋다', '훌륭하다', '멋있다', '우수하다', '성공', '증가', '개선', '긍정', '최고', '상승', '호황', '발전', '진보', '기회']
        negative_words = ['나쁘다', '끔찍하다', '실패', '감소', '악화', '부정', '최악', '위기', '문제', '하락', '불황', '침체', '위험', '손실']
        
        text_lower = text.lower()
        positive_count = sum(1 for word in positive_words if word in text_lower)
        negative_count = sum(1 for word in negative_words if word in text_lower)
        
        # 감정 판정
        if positive_count > negative_count:
            sentiment = "긍정 😊"
            confidence = min(positive_count / (positive_count + negative_count + 1), 1.0)
        elif negative_count > positive_count:
            sentiment = "부정 😔"
            confidence = min(negative_count / (positive_count + negative_count + 1), 1.0)
        else:
            sentiment = "중립 😐"
            confidence = 0.5
        
        return {
            'sentiment': sentiment,
            'confidence': round(confidence, 2),
            'score': polarity
        }
    
    except Exception as e:
        return {
            'sentiment': '분석불가',
            'confidence': 0,
            'score': 0
        }

def extract_keywords(text, num_keywords=5):
    """
    텍스트에서 주요 키워드 추출
    
    Args:
        text: 분석할 텍스트
        num_keywords: 추출할 키워드 개수
    
    Returns:
        list: 키워드 리스트
    """
    try:
        # 간단한 명사 추출 (공백 기준, 길이 2 이상)
        words = text.split()
        nouns = [word for word in words if len(word) >= 2 and not word.isdigit()]
        
        # 불용어 제거
        stop_words = {'것', '수', '등', '년', '월', '일', '개', '나', '그', '하지만', '그런데', '있는', '한다', '했다', '하는', '할'}
        keywords = [word for word in nouns if word not in stop_words]
        
        # 단어 빈도 기반 정렬
        from collections import Counter
        keyword_freq = Counter(keywords)
        top_keywords = [word for word, _ in keyword_freq.most_common(num_keywords)]
        
        return top_keywords if top_keywords else ['분석불가']
    
    except Exception as e:
        return ['분석오류']

def clean_text(text):
    """
    텍스트 정제
    """
    text = re.sub(r'<[^>]+>', '', text)  # HTML 태그 제거
    text = re.sub(r'[^ㄱ-ㅣ가-힣a-zA-Z0-9\s\.\,\!\?]', '', text)  # 특수문자 제거
    return text.strip()
