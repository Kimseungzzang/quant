package com.quant.api.dto;

import com.quant.api.domain.Position;
import lombok.Builder;
import lombok.Getter;

import java.math.BigDecimal;
import java.time.OffsetDateTime;

@Getter
@Builder
public class PositionDto {
    private Long id;
    private String stockCode;
    private String stockName;
    private String market;
    private Integer quantity;
    private BigDecimal avgPrice;
    private String currency;
    private BigDecimal currentPrice;
    private BigDecimal marketValue;
    private BigDecimal unrealizedPnl;
    private BigDecimal unrealizedPct;
    private String mode;
    private OffsetDateTime openedAt;
    private OffsetDateTime updatedAt;

    public static PositionDto from(Position p) {
        BigDecimal current = p.getCurrentPrice() != null ? p.getCurrentPrice() : p.getAvgPrice();
        BigDecimal marketValue = current.multiply(BigDecimal.valueOf(p.getQuantity()));
        return PositionDto.builder()
            .id(p.getId())
            .stockCode(p.getStockCode())
            .stockName(p.getStockName())
            .market(p.getMarket())
            .quantity(p.getQuantity())
            .avgPrice(p.getAvgPrice())
            .currency(p.getCurrency())
            .currentPrice(current)
            .marketValue(marketValue)
            .unrealizedPnl(p.getUnrealizedPnl())
            .unrealizedPct(p.getUnrealizedPct())
            .mode(p.getMode())
            .openedAt(p.getOpenedAt())
            .updatedAt(p.getUpdatedAt())
            .build();
    }
}
